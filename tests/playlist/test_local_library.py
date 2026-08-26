from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mammamiradio.core.models import PlaylistSource, StationState, Track
from mammamiradio.playlist.local_library import (
    reconcile_local_library,
    scan_and_reconcile_local_library,
    scan_local_library,
)


def _config(primary: Path, *legacy: Path):
    return SimpleNamespace(music_dir=primary, legacy_music_dirs=legacy, is_addon=False)


def _track(title: str, *, source="starter", path: Path | None = None) -> Track:
    return Track(title=title, artist="Artist", duration_ms=180_000, source=source, local_path=path)


def test_scan_is_recursive_case_insensitive_and_supports_common_audio(tmp_path):
    primary = tmp_path / "primary"
    legacy = tmp_path / "legacy"
    album = primary / "Artist" / "Album"
    album.mkdir(parents=True)
    legacy.mkdir()
    titles = ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight")
    suffixes = ("MP3", "flac", "OpUs", "m4a", "AAC", "ogg", "WAV", "MP4")
    for title, suffix in zip(titles, suffixes, strict=True):
        (album / f"Artist - {title}.{suffix}").write_bytes(b"audio")
    (album / "cover.jpg").write_bytes(b"image")
    (album / "empty.wav").touch()
    (album / "linked.mp3").symlink_to(album / "Artist - One.MP3")
    (linked_root := tmp_path / "linked-root").symlink_to(primary, target_is_directory=True)
    (legacy / "Artist - One.mp3").write_bytes(b"duplicate")

    result = scan_local_library(_config(primary, legacy, linked_root))

    assert result.complete is False
    assert {track.title for track in result.tracks} == set(titles)
    assert next(track.local_path for track in result.tracks if track.title == "One") == album / "Artist - One.MP3"
    assert result.ignored == {"duplicate": 1, "empty_file": 1, "symlink": 1, "unsupported_format": 1}
    assert result.warnings == [f"Symlinked music folder skipped: {linked_root}. Use its real path; tracks kept."]


@pytest.mark.parametrize("fail_on_stat", [False, True])
def test_entry_cap_bounds_iterator_and_unreadable_entries(tmp_path, fail_on_stat):
    consumed = 0

    def unreadable(**_):
        raise OSError("unreadable")

    def entries():
        nonlocal consumed
        for index in range(1_000):
            consumed += 1
            name = "Artist - Broken.mp3" if index == 0 else f"ignored-{index:04d}.txt"
            yield SimpleNamespace(
                name=name,
                path=str(tmp_path / name),
                is_symlink=lambda: False,
                is_dir=lambda **_: False,
                is_file=unreadable if index == 0 and not fail_on_stat else lambda **_: True,
                stat=unreadable if index == 0 and fail_on_stat else lambda **_: SimpleNamespace(st_size=1),
            )

    with (
        patch("mammamiradio.playlist.local_library.MAX_LOCAL_LIBRARY_ENTRIES", 2),
        patch("mammamiradio.playlist.local_library.os.scandir", return_value=nullcontext(entries())),
    ):
        result = scan_local_library(_config(tmp_path))

    assert consumed == 3
    assert result.entries_seen == 2
    assert result.ignored == {"unreadable": 1, "unsupported_format": 1}
    assert result.complete is False
    assert "More than 2 entries" in result.warnings[-1]


def test_complete_reconcile_updates_only_managed_local_membership(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    old_path = root / "Artist - Old.mp3"
    new_path = root / "Artist - New.mp3"
    banned_path = root / "Artist - Banned.mp3"
    new_path.write_bytes(b"audio")
    banned_path.write_bytes(b"audio")
    unrelated_path = tmp_path / "one-off.mp3"
    state = StationState(
        playlist=[
            _track("Starter"),
            _track("Old", source="local", path=old_path),
            _track("One off", source="local", path=unrelated_path),
        ],
        playlist_source=PlaylistSource(kind="demo", label="Starter", track_count=1),
        blocklist={("artist", "banned"): {"display": "Artist - Banned"}},
    )
    result = scan_local_library(_config(root))
    with (
        patch.object(Path, "resolve", side_effect=AssertionError("resolve during reconcile")),
        patch.object(Path, "exists", side_effect=AssertionError("exists during reconcile")),
    ):
        outcome = reconcile_local_library(state, result)

    assert [track.title for track in state.playlist] == ["Starter", "One off", "New"]
    assert outcome["added"] == outcome["removed"] == outcome["active"] == outcome["playlist_revision"] == 1
    assert outcome["banned"] == 1
    assert state.playlist_source.kind == "demo"

    state = StationState(
        playlist=[_track("Old", source="local", path=old_path)],
        blocklist={("artist", "banned"): {"display": "Artist - Banned"}},
    )
    scan = replace(
        result,
        complete=False,
        warnings=["The mounted library could not be read completely."],
    )
    outcome = reconcile_local_library(state, scan)
    assert [track.title for track in state.playlist] == ["Old", "New"]
    assert outcome["removed"] == 0
    assert outcome["added"] == 1


@pytest.mark.asyncio
async def test_scan_wrapper_coalesces_and_hides_failures(tmp_path):
    app_state = SimpleNamespace(
        local_library_scan_lock=asyncio.Lock(),
        local_library_status={"in_progress": False},
        source_switch_lock=asyncio.Lock(),
        config=_config(tmp_path),
    )
    async with app_state.local_library_scan_lock:
        busy = await scan_and_reconcile_local_library(app_state)
    assert busy["in_progress"] is busy["already_in_progress"] is True

    with patch("mammamiradio.playlist.local_library.scan_local_library", side_effect=OSError("raw failure")):
        failed = await scan_and_reconcile_local_library(app_state)
    assert failed["in_progress"] is False
    assert failed["error"] == "Check the local music folders, then select Scan now."

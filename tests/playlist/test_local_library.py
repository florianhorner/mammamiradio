from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mammamiradio.core.models import PlaylistSource, StationState, Track
from mammamiradio.playlist.local_library import reconcile_local_library, scan_local_library


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
    symlink = album / "linked.mp3"
    symlink.symlink_to(album / "Artist - One.MP3")
    (legacy / "Artist - One.mp3").write_bytes(b"duplicate")

    result = scan_local_library(_config(primary, legacy))

    assert result.complete is True
    assert {track.title for track in result.tracks} == set(titles)
    assert next(track.local_path for track in result.tracks if track.title == "One") == album / "Artist - One.MP3"
    assert result.ignored == {"duplicate": 1, "empty_file": 1, "symlink": 1, "unsupported_format": 1}


def test_entry_cap_bounds_directory_iterator_before_sorting(tmp_path):
    consumed = 0

    def entries():
        nonlocal consumed
        for index in range(1_000):
            consumed += 1
            name = f"ignored-{index:04d}.txt"
            yield SimpleNamespace(
                name=name,
                path=str(tmp_path / name),
                is_symlink=lambda: False,
                is_dir=lambda **_: False,
                is_file=lambda **_: True,
            )

    with (
        patch("mammamiradio.playlist.local_library.MAX_LOCAL_LIBRARY_ENTRIES", 2),
        patch("mammamiradio.playlist.local_library.os.scandir", return_value=nullcontext(entries())),
    ):
        result = scan_local_library(_config(tmp_path))

    assert consumed == 3
    assert result.entries_seen == 2
    assert result.ignored == {"unsupported_format": 2}
    assert result.complete is False
    assert "more than 2 entries" in result.warnings[-1]


def test_complete_reconcile_updates_only_managed_local_membership(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    old_path = root / "Artist - Old.mp3"
    new_path = root / "Artist - New.mp3"
    new_path.write_bytes(b"audio")
    unrelated_path = tmp_path / "one-off.mp3"
    state = StationState(
        playlist=[
            _track("Starter"),
            _track("Old", source="local", path=old_path),
            _track("One off", source="local", path=unrelated_path),
        ],
        playlist_source=PlaylistSource(kind="demo", label="Starter", track_count=1),
    )
    result = scan_local_library(_config(root))
    with (
        patch.object(Path, "resolve", side_effect=AssertionError("resolve during reconcile")),
        patch.object(Path, "exists", side_effect=AssertionError("exists during reconcile")),
    ):
        outcome = reconcile_local_library(state, result)

    assert [track.title for track in state.playlist] == ["Starter", "One off", "New"]
    assert outcome["added"] == outcome["removed"] == outcome["active"] == outcome["playlist_revision"] == 1
    assert outcome["banned"] == state.source_revision == state.continuity_epoch == 0
    assert state.playlist_source.kind == "demo"
    assert state.source_readiness.entries["local"].configured is True

    state = StationState(playlist=[_track("Old", source="local", path=old_path)])
    scan = replace(
        result,
        complete=False,
        warnings=["The mounted library could not be read completely."],
    )
    outcome = reconcile_local_library(state, scan)
    assert [track.title for track in state.playlist] == ["Old", "New"]
    assert outcome["removed"] == 0
    assert outcome["added"] == 1

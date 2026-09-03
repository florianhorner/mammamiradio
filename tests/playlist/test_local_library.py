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


def _config(root: Path):
    return SimpleNamespace(music_dir=root)


def _track(title: str, *, source="starter", path: Path | None = None) -> Track:
    return Track(title=title, artist="Artist", duration_ms=180_000, source=source, local_path=path)


def test_scan_is_recursive_case_insensitive_and_supports_common_audio(tmp_path):
    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    duplicate = root / "0-Duplicate"
    duplicate.mkdir()
    titles = ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight")
    suffixes = ("MP3", "flac", "OpUs", "m4a", "AAC", "ogg", "WAV", "MP4")
    for title, suffix in zip(titles, suffixes, strict=True):
        (album / f"Artist - {title}.{suffix}").write_bytes(b"audio")
    (album / "cover.jpg").write_bytes(b"image")
    (album / "empty.wav").touch()
    (album / "linked.mp3").symlink_to(album / "Artist - One.MP3")
    (duplicate / "Artist - One.mp3").write_bytes(b"duplicate")

    result = scan_local_library(_config(root))

    assert result.complete is True
    assert {track.title for track in result.tracks} == set(titles)
    assert next(track.local_path for track in result.tracks if track.title == "One") == album / "Artist - One.MP3"
    assert result.ignored == {"duplicate": 1, "empty_file": 1, "symlink": 1, "unsupported_format": 1}
    assert result.warnings == []


def test_scan_rejects_a_symlinked_configured_root(tmp_path):
    real_root = tmp_path / "music"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    result = scan_local_library(_config(linked_root))

    assert result.complete is False
    assert result.tracks == []
    assert result.warnings == [f"Symlinked music folder skipped: {linked_root}. Use its real path; tracks kept."]


def test_scanned_track_cannot_escape_library_after_symlink_swap(tmp_path):
    from mammamiradio.playlist.downloader import _resolve_cached_or_local

    root = tmp_path / "music"
    root.mkdir()
    scanned_path = root / "Artist - Song.mp3"
    scanned_path.write_bytes(b"inside")
    track = scan_local_library(_config(root)).tracks[0]
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    scanned_path.unlink()
    scanned_path.symlink_to(outside)
    cache = tmp_path / "cache"
    cache.mkdir()

    assert _resolve_cached_or_local(track, cache, root) is None


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
    """Reconcile rewrites membership and promotes kind to local (crate composition).

    ``playlist_source.kind`` names what is in the crate, not how it was loaded.
    Overlaying operator files onto a starter/demo bag must flip kind to ``local``
    so selection and operator copy agree that local is the base.
    """
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
        playlist_source=PlaylistSource(kind="starter", label="Starter", track_count=1),
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
    assert state.playlist_source.kind == "local"
    assert state.playlist_source.label == "Local music"

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


def test_probe_prefers_ffprobe_tags_and_duration(tmp_path):
    """Tagged files air with tag title/artist and real duration, not Unknown + 3:30."""
    root = tmp_path / "music"
    root.mkdir()
    path = root / "25-marco-buono-a-love-like-this.mp3"
    path.write_bytes(b"audio")

    def _fake_ffprobe(cmd, **_kwargs):
        assert cmd[0] == "ffprobe"
        return SimpleNamespace(
            returncode=0,
            stdout='{"format":{"duration":"187.5","tags":{"artist":"Marco Buono","title":"A Love Like This"}}}',
            stderr="",
        )

    with patch("mammamiradio.playlist.local_library.subprocess.run", side_effect=_fake_ffprobe):
        result = scan_local_library(_config(root))

    assert len(result.tracks) == 1
    track = result.tracks[0]
    assert track.artist == "Marco Buono"
    assert track.title == "A Love Like This"
    assert track.duration_ms == 187_500
    assert track.display == "Marco Buono – A Love Like This"


def test_probe_falls_back_to_filename_when_tags_empty(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "Artist - Song.mp3"
    path.write_bytes(b"audio")

    def _fake_ffprobe(cmd, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout='{"format":{"duration":"0","tags":{"artist":"  ","title":""}}}',
            stderr="",
        )

    with patch("mammamiradio.playlist.local_library.subprocess.run", side_effect=_fake_ffprobe):
        result = scan_local_library(_config(root))

    track = result.tracks[0]
    assert track.artist == "Artist"
    assert track.title == "Song"
    assert track.duration_ms == 210_000


def test_probe_falls_back_when_ffprobe_unavailable(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    (root / "plain-hyphen-name.mp3").write_bytes(b"audio")

    with patch("mammamiradio.playlist.local_library.subprocess.run", side_effect=FileNotFoundError("ffprobe")):
        result = scan_local_library(_config(root))

    track = result.tracks[0]
    assert track.artist == "Unknown"
    assert track.title == "plain-hyphen-name"
    assert track.duration_ms == 210_000


def test_mixed_reconcile_reaches_weighted_selector_with_local_in_candidates(tmp_path):
    """S6-3 seam: reconcile → select_next_track must reach weighted selection.

    Deterministic: assert ``random.choices`` is invoked and the local track is
    among its candidates. Fails if the starter bag-order early return still
    swallows a mixed crate.
    """
    root = tmp_path / "music"
    root.mkdir()
    local_path = root / "Operator - Local.mp3"
    local_path.write_bytes(b"audio")
    starter = _track("Starter One", source="starter")
    state = StationState(
        playlist=[starter],
        playlist_source=PlaylistSource(kind="starter", label="Starter", track_count=1),
    )
    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        return_value=SimpleNamespace(returncode=1, stdout="", stderr="fail"),
    ):
        scan = scan_local_library(_config(root))
    reconcile_local_library(state, scan)

    assert state.playlist_source.kind == "local"
    assert any(track.source == "local" for track in state.playlist)

    captured: dict = {}

    def _choose(candidates, **kwargs):
        captured["candidates"] = list(candidates)
        captured["weights"] = list(kwargs["weights"])
        local = next(track for track in candidates if track.source == "local")
        return [local]

    with patch("mammamiradio.core.models.random.choices", side_effect=_choose) as choices:
        picked = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)

    choices.assert_called_once()
    assert any(track.source == "local" for track in captured["candidates"])
    assert picked.source == "local"
    local_idx = next(i for i, track in enumerate(captured["candidates"]) if track.source == "local")
    starter_idx = next(i for i, track in enumerate(captured["candidates"]) if track.source == "starter")
    assert captured["weights"][local_idx] > captured["weights"][starter_idx]


def test_mixed_pool_local_share_and_starter_no_repeat():
    """Acceptance: 30 local + 12 starter → ≥80% local in 50 picks; no starter repeat early."""
    import random

    locals_ = [
        Track(
            title=f"Local {i}",
            artist=f"Local Artist {i}",
            duration_ms=180_000,
            source="local",
            local_path=Path(f"/music/local-{i}.mp3"),
        )
        for i in range(30)
    ]
    starters = [
        Track(
            title=f"Starter {i}",
            artist=f"Starter Artist {i}",
            duration_ms=180_000,
            source="starter",
            spotify_id=f"starter-{i}",
        )
        for i in range(12)
    ]
    state = StationState(
        playlist=[*starters, *locals_],
        playlist_source=PlaylistSource(kind="local", label="Local music", track_count=42),
    )

    random.seed(0)
    selected: list[Track] = []
    starter_seen: set[str] = set()
    for i in range(50):
        track = state.select_next_track(repeat_cooldown=0, artist_cooldown=0, max_artist_per_hour=0)
        if track.source == "starter":
            assert track.cache_key not in starter_seen, f"starter repeated before full cycle at pick {i}"
            starter_seen.add(track.cache_key)
            reservation_id = f"r{i}"
            assert state.reserve_music_admission(reservation_id, track) is True
            assert state.commit_music_admission(reservation_id) is True
        state.after_music(track)
        selected.append(track)

    local_count = sum(1 for track in selected if track.source == "local")
    assert local_count / 50 >= 0.80
    assert len(starter_seen) == sum(1 for track in selected if track.source == "starter")


def test_complete_reconcile_demotes_kind_when_locals_leave(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    local_path = root / "Artist - Song.mp3"
    local_path.write_bytes(b"audio")
    starter = _track("Starter", source="starter")
    state = StationState(
        playlist=[starter, _track("Song", source="local", path=local_path)],
        playlist_source=PlaylistSource(kind="local", label="Local music", track_count=2),
    )
    local_path.unlink()
    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
    ):
        scan = scan_local_library(_config(root))
    outcome = reconcile_local_library(state, scan)
    assert outcome["active"] == 0
    assert outcome["removed"] == 1
    assert [track.title for track in state.playlist] == ["Starter"]
    assert state.playlist_source.kind == "starter"
    assert state.playlist_source.label == "Bundled starter music"


def test_same_identity_refresh_updates_duration(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "Artist - Song.mp3"
    path.write_bytes(b"audio")
    existing = Track(
        title="Song",
        artist="Artist",
        duration_ms=210_000,
        source="local",
        local_path=path,
    )
    state = StationState(
        playlist=[existing],
        playlist_source=PlaylistSource(kind="local", label="Local music", track_count=1),
    )

    def _fake_ffprobe(cmd, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout='{"format":{"duration":"201.0","tags":{"artist":"Artist","title":"Song"}}}',
            stderr="",
        )

    with patch("mammamiradio.playlist.local_library.subprocess.run", side_effect=_fake_ffprobe):
        scan = scan_local_library(_config(root))
    reconcile_local_library(state, scan)
    assert state.playlist[0] is existing
    assert existing.duration_ms == 201_000

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mammamiradio.core.models import PlaylistSource, StationState, Track
from mammamiradio.playlist import local_library
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

    ``playlist_source.kind`` names what is in the crate for a provider-free bag
    kind, not how it was loaded.
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


def _ffprobe_ok(*, artist: str, title: str, seconds: str):
    def _run(cmd, **_kwargs):
        assert cmd[0] == "ffprobe"
        payload = {"format": {"duration": seconds, "tags": {"artist": artist, "title": title}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    return _run


def test_transient_probe_failure_keeps_a_measured_duration(tmp_path):
    """A timeout is not evidence that a 4:05 song became a 3:30 one.

    Reachable whenever the filename already encodes the tags — the common case
    for a tidy library. The identity then still matches on a failed probe, so
    the refresh branch fires and the nonzero 3:30 fallback overwrites the real
    duration. Up Next and every runway estimate read that number.
    """
    root = tmp_path / "music"
    root.mkdir()
    path = root / "Marco Buono - A Love Like This.mp3"
    path.write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=_ffprobe_ok(artist="Marco Buono", title="A Love Like This", seconds="245.0"),
    ):
        first = scan_local_library(_config(root))
    state = StationState(playlist=[], playlist_source=PlaylistSource(kind="starter", label="Starter"))
    reconcile_local_library(state, first)

    tagged = state.playlist[0]
    assert (tagged.artist, tagged.title, tagged.duration_ms) == ("Marco Buono", "A Love Like This", 245_000)

    # Rewrite the file so the memo misses, then time the probe out.
    path.write_bytes(b"audio-changed")
    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=subprocess.TimeoutExpired("ffprobe", 5.0),
    ):
        second = scan_local_library(_config(root))
    assert second.probe_ok_paths == set()
    reconcile_local_library(state, second)

    assert state.playlist[0] is tagged
    assert tagged.duration_ms == 245_000


def test_transient_probe_failure_does_not_report_a_tagged_track_as_removed(tmp_path):
    """The other half: a failed probe must not flip the track's identity.

    With tags the filename does not encode, a failed probe reads back as
    ``Unknown``. That is a different identity, so reconcile treated the file as
    a swap and reported the tagged track removed — feeding a still-present
    track to the readiness ledger's removal path.
    """
    root = tmp_path / "music"
    root.mkdir()
    path = root / "25-track.mp3"
    path.write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=_ffprobe_ok(artist="Marco Buono", title="A Love Like This", seconds="245.0"),
    ):
        first = scan_local_library(_config(root))
    state = StationState(playlist=[], playlist_source=PlaylistSource(kind="starter", label="Starter"))
    reconcile_local_library(state, first)
    tagged = state.playlist[0]

    path.write_bytes(b"audio-changed")
    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=subprocess.TimeoutExpired("ffprobe", 5.0),
    ):
        second = scan_local_library(_config(root))
    outcome = reconcile_local_library(state, second)

    assert outcome["removed"] == 0
    assert outcome["added"] == 0
    assert state.playlist == [tagged]
    assert (tagged.artist, tagged.title) == ("Marco Buono", "A Love Like This")


def test_probe_is_memoized_for_unchanged_files_across_scans(tmp_path):
    """The scanner wakes every 60s; re-probing every unchanged file is a CPU storm."""
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist - Song.mp3").write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=_ffprobe_ok(artist="Artist", title="Song", seconds="120.0"),
    ) as run:
        first = scan_local_library(_config(root))
        second = scan_local_library(_config(root))

    assert run.call_count == 1
    assert first.tracks[0].duration_ms == second.tracks[0].duration_ms == 120_000
    assert second.probe_ok_paths == first.probe_ok_paths


def test_exhausted_probe_budget_defers_files_instead_of_guessing(tmp_path):
    """Out of budget, a file is deferred — not admitted under a filename guess.

    Admitting it would put a fabricated 3:30 in Up Next and hand a banned song
    a fresh identity, and marking the whole scan incomplete would stop the
    station noticing files the operator actually deleted.
    """
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist - Song.mp3").write_bytes(b"audio")

    with (
        patch.object(local_library, "_SCAN_PROBE_BUDGET_SEC", 0.0),
        patch("mammamiradio.playlist.local_library.subprocess.run") as run,
    ):
        result = scan_local_library(_config(root))

    run.assert_not_called()
    assert result.tracks == []
    assert len(result.deferred_paths) == 1
    assert result.complete is True

    # A deferred file is not gone: an existing track for that path survives.
    state = StationState(
        playlist=[_track("Song", source="local", path=root / "Artist - Song.mp3")],
        playlist_source=PlaylistSource(kind="starter", label="Starter"),
    )
    outcome = reconcile_local_library(state, result)
    assert outcome == {**outcome, "removed": 0, "added": 0}
    assert len(state.playlist) == 1


def test_probe_budget_resumes_across_passes_until_every_file_is_read(tmp_path):
    """The budget defers work; it must not abandon it."""
    root = tmp_path / "music"
    root.mkdir()
    for index in range(4):
        (root / f"Artist - Song {index}.mp3").write_bytes(b"audio")

    calls = {"n": 0}

    def _slow_ffprobe(cmd, **_kwargs):
        calls["n"] += 1
        time.sleep(0.05)
        # Distinct tags per file, or three of the four drop out as duplicates.
        title = Path(cmd[-1]).stem.split(" - ", 1)[1]
        payload = {"format": {"duration": "120.0", "tags": {"artist": "Artist", "title": title}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    with (
        patch.object(local_library, "_SCAN_PROBE_BUDGET_SEC", 0.06),
        patch("mammamiradio.playlist.local_library.subprocess.run", side_effect=_slow_ffprobe),
    ):
        first = scan_local_library(_config(root))
        assert first.deferred_paths, "budget should not have covered all four files"
        first_reads = calls["n"]

        # Later passes serve the memo for what pass one read, so the budget is
        # spent only on files still unread — the scan converges.
        for _ in range(6):
            latest = scan_local_library(_config(root))
            if not latest.deferred_paths:
                break
        else:  # pragma: no cover - convergence failure
            raise AssertionError("probe budget never caught up")

    assert len(latest.tracks) == 4
    assert len(latest.probe_ok_paths) == 4
    assert calls["n"] > first_reads

    # Fully warm: a further pass re-reads nothing at all.
    with patch("mammamiradio.playlist.local_library.subprocess.run") as run:
        warm = scan_local_library(_config(root))
    run.assert_not_called()
    assert len(warm.tracks) == 4


def test_a_successful_probe_without_a_duration_keeps_the_measured_one(tmp_path):
    """probe_ok is not measured_duration: a tagged file may report no duration.

    Separates the two provenance sets. Without the ``measured_duration_paths``
    gate the 3:30 default would overwrite a real 4:05 even though the probe
    itself succeeded.
    """
    root = tmp_path / "music"
    root.mkdir()
    path = root / "Marco Buono - A Love Like This.mp3"
    path.write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=_ffprobe_ok(artist="Marco Buono", title="A Love Like This", seconds="245.0"),
    ):
        first = scan_local_library(_config(root))
    state = StationState(playlist=[], playlist_source=PlaylistSource(kind="starter", label="Starter"))
    reconcile_local_library(state, first)
    tagged = state.playlist[0]
    assert tagged.duration_ms == 245_000

    path.write_bytes(b"audio-changed")

    def _no_duration(cmd, **_kwargs):
        payload = {"format": {"tags": {"artist": "Marco Buono", "title": "A Love Like This"}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    with patch("mammamiradio.playlist.local_library.subprocess.run", side_effect=_no_duration):
        second = scan_local_library(_config(root))

    assert second.probe_ok_paths and second.measured_duration_paths == set()
    assert second.tracks[0].duration_ms == 210_000

    reconcile_local_library(state, second)
    assert state.playlist[0] is tagged
    assert tagged.duration_ms == 245_000


def test_probe_failure_does_not_let_a_filename_guess_trip_the_blocklist(tmp_path):
    """A ban is judged on identity, and a failed probe only has a guess.

    The file is named after a banned song but tagged as something else. Judging
    the ban on the guess drops it from the scan, which reads downstream as "the
    file is gone" and deletes a live, legitimate track.
    """
    root = tmp_path / "music"
    root.mkdir()
    path = root / "Banned Artist - Banned Song.mp3"
    path.write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=_ffprobe_ok(artist="Real Artist", title="Real Song", seconds="200.0"),
    ):
        first = scan_local_library(_config(root))
    state = StationState(
        playlist=[],
        playlist_source=PlaylistSource(kind="starter", label="Starter"),
        blocklist={("banned artist", "banned song"): {"display": "Banned Artist - Banned Song"}},
    )
    reconcile_local_library(state, first)
    tagged = state.playlist[0]
    assert (tagged.artist, tagged.title) == ("Real Artist", "Real Song")

    path.write_bytes(b"audio-changed")
    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=subprocess.TimeoutExpired("ffprobe", 5.0),
    ):
        second = scan_local_library(_config(root))
    outcome = reconcile_local_library(state, second)

    assert outcome["removed"] == 0
    assert outcome["banned"] == 0
    assert state.playlist == [tagged]


def test_a_failed_probe_is_never_memoized(tmp_path):
    """Caching a failure would make a transient rename permanent."""
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist - Song.mp3").write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=subprocess.TimeoutExpired("ffprobe", 5.0),
    ) as failing:
        scan_local_library(_config(root))
    assert failing.call_count == 1
    assert local_library._probe_cache == {}

    # Same file, untouched: the next pass must try again rather than serve the
    # filename guess it fell back to.
    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=_ffprobe_ok(artist="Tagged", title="Recovered", seconds="200.0"),
    ) as recovering:
        result = scan_local_library(_config(root))
    assert recovering.call_count == 1
    assert (result.tracks[0].artist, result.tracks[0].title) == ("Tagged", "Recovered")


def test_a_temporarily_unavailable_root_keeps_the_probe_memo(tmp_path):
    """A USB unplug or a slow mount must not cost the whole warm library."""
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist - Song.mp3").write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=_ffprobe_ok(artist="Artist", title="Song", seconds="120.0"),
    ) as run:
        scan_local_library(_config(root))
        assert run.call_count == 1
        warm = dict(local_library._probe_cache)
        assert len(warm) == 1

        # The folder disappears for one pass, then comes back.
        root.rename(tmp_path / "unplugged")
        unavailable = scan_local_library(_config(root))
        assert unavailable.has_available_root is False
        assert local_library._probe_cache == warm

        (tmp_path / "unplugged").rename(root)
        scan_local_library(_config(root))

    assert run.call_count == 1


def test_a_completed_pass_drops_memo_entries_for_deleted_files(tmp_path):
    """The memo is bounded by pruning, not only by the cap."""
    root = tmp_path / "music"
    root.mkdir()
    keep = root / "Artist - Keep.mp3"
    drop = root / "Artist - Drop.mp3"
    keep.write_bytes(b"audio")
    drop.write_bytes(b"audio")

    def _tagged(cmd, **_kwargs):
        payload = {"format": {"duration": "120.0", "tags": {"artist": "Artist", "title": Path(cmd[-1]).stem}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    with patch("mammamiradio.playlist.local_library.subprocess.run", side_effect=_tagged):
        scan_local_library(_config(root))
        assert len(local_library._probe_cache) == 2
        drop.unlink()
        scan_local_library(_config(root))

    assert list(local_library._probe_cache) == [local_library._path_key(keep)]


def test_memo_misses_when_size_changes_but_mtime_does_not(tmp_path):
    """Both halves of the memo key are load-bearing."""
    root = tmp_path / "music"
    root.mkdir()
    path = root / "Artist - Song.mp3"
    path.write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=_ffprobe_ok(artist="Artist", title="Song", seconds="120.0"),
    ) as run:
        scan_local_library(_config(root))
        before = path.stat()
        path.write_bytes(b"audio-with-a-longer-body")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_mtime_ns == before.st_mtime_ns
        scan_local_library(_config(root))

    assert run.call_count == 2


def test_probe_memo_survives_concurrent_scans(tmp_path):
    """Two threads reach this module: the 60s scanner and producer recovery.

    Only the scanner holds ``local_library_scan_lock``, so pruning used to
    iterate the memo while the other thread inserted into it and raise
    ``dictionary changed size during iteration`` into the generation cycle.
    """
    root = tmp_path / "music"
    root.mkdir()
    for index in range(40):
        (root / f"Artist - Song {index}.mp3").write_bytes(b"audio")

    local_library._probe_cache.update({f"/stale/path/{index}": (1, 1, "A", "B", 1000) for index in range(20_000)})
    errors: list[BaseException] = []

    def _prune_repeatedly():
        try:
            for _ in range(40):
                local_library._prune_probe_cache(set())
        except BaseException as exc:  # pragma: no cover - the bug being pinned
            errors.append(exc)

    def _scan_repeatedly():
        try:
            with patch(
                "mammamiradio.playlist.local_library.subprocess.run",
                side_effect=_ffprobe_ok(artist="Artist", title="Song", seconds="120.0"),
            ):
                for _ in range(40):
                    local_library._probe_cache.clear()
                    scan_local_library(_config(root))
        except BaseException as exc:  # pragma: no cover - the bug being pinned
            errors.append(exc)

    threads = [threading.Thread(target=_prune_repeatedly), threading.Thread(target=_scan_repeatedly)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_local_overlay_never_rewrites_a_charts_kind(tmp_path):
    """charts also selects a provider: rewriting it disabled chart refresh forever.

    ``producer`` refreshes the chart playlist only while kind is ``charts``, and
    the demotion path can only ever restore ``starter`` — so the flip was
    one-way. Composition rides on ``local_overlay`` and ``Track.source``.
    """
    root = tmp_path / "music"
    root.mkdir()
    (root / "Operator - Local.mp3").write_bytes(b"audio")
    state = StationState(
        playlist=[_track("Chart One", source="charts")],
        playlist_source=PlaylistSource(kind="charts", label="Top 50", track_count=1),
    )

    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=_ffprobe_ok(artist="Operator", title="Local", seconds="200.0"),
    ):
        scan = scan_local_library(_config(root))
    reconcile_local_library(state, scan)

    assert state.playlist_source.kind == "charts"
    assert state.playlist_source.label == "Top 50"
    assert state.playlist_source.source_id == ""
    assert any(track.source == "local" for track in state.playlist)


def test_local_overlay_still_promotes_a_starter_bag(tmp_path):
    """The bag kinds carry no provider, so composition promotion stays intact."""
    root = tmp_path / "music"
    root.mkdir()
    (root / "Operator - Local.mp3").write_bytes(b"audio")
    state = StationState(
        playlist=[_track("Starter One", source="starter")],
        playlist_source=PlaylistSource(kind="starter", label="Starter", track_count=1),
    )

    with patch(
        "mammamiradio.playlist.local_library.subprocess.run",
        side_effect=_ffprobe_ok(artist="Operator", title="Local", seconds="200.0"),
    ):
        scan = scan_local_library(_config(root))
    reconcile_local_library(state, scan)

    assert state.playlist_source.kind == "local"
    assert state.playlist_source.label == "Local music"

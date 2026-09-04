from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mammamiradio.core.models import PlaylistSource, StationState, Track
from mammamiradio.playlist import local_library as local_library_module
from mammamiradio.playlist.local_library import (
    _probe_local_audio_metadata,
    legacy_local_identity_key,
    local_track_is_blocklisted,
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
    one = next(track for track in result.tracks if track.title == "One")
    assert one.artist == "Artist"
    assert one.local_path == album / "Artist - One.MP3"
    assert result.ignored == {"duplicate": 1, "empty_file": 1, "symlink": 1, "unsupported_format": 1}
    assert result.warnings == []


def test_scan_prefers_embedded_metadata_over_filename(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "Filename Artist - Filename Title.mp3"
    path.write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library._probe_local_audio_metadata",
        return_value=("Tagged Artist", "Tagged Title"),
    ):
        track = scan_local_library(_config(root)).tracks[0]

    assert track.artist == "Tagged Artist"
    assert track.title == "Tagged Title"


def test_scan_fills_missing_embedded_field_from_exact_filename(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "Filename Artist - Filename Title.mp3"
    path.write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library._probe_local_audio_metadata",
        return_value=("", "Tagged Title"),
    ):
        track = scan_local_library(_config(root)).tracks[0]

    assert track.artist == "Filename Artist"
    assert track.title == "Tagged Title"


def test_scan_humanizes_untagged_slug_without_inventing_artist(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "29-salvatore-on-everything.mp3"
    path.write_bytes(b"audio")

    with patch("mammamiradio.playlist.local_library._probe_local_audio_metadata", return_value=("", "")):
        track = scan_local_library(_config(root)).tracks[0]

    assert track.artist == ""
    assert track.title == "Salvatore On Everything"


def test_probe_reads_format_tags_before_stream_tags(tmp_path):
    path = tmp_path / "tagged.mp3"
    response = type(
        "ProbeResponse",
        (),
        {
            "returncode": 0,
            "stdout": json.dumps(
                {
                    "format": {"tags": {"TITLE": "Format Title"}},
                    "streams": [{"codec_type": "audio", "tags": {"artist": "Stream Artist", "title": "Stream Title"}}],
                }
            ),
        },
    )()

    with patch("mammamiradio.playlist.local_library.subprocess.run", return_value=response):
        metadata = _probe_local_audio_metadata(path)

    assert metadata == ("Stream Artist", "Format Title")


def test_scan_keeps_file_when_ffprobe_is_unavailable(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "No Tags Here.mp3"
    path.write_bytes(b"audio")

    with patch("mammamiradio.playlist.local_library.subprocess.run", side_effect=FileNotFoundError):
        result = scan_local_library(_config(root))

    assert result.complete is True
    assert result.tracks[0].artist == ""
    assert result.tracks[0].title == "No Tags Here"


@pytest.mark.parametrize(
    "response",
    [
        type("ProbeResponse", (), {"returncode": 1, "stdout": ""})(),
        type("ProbeResponse", (), {"returncode": 0, "stdout": "not json"})(),
    ],
)
def test_scan_keeps_file_when_metadata_probe_fails(tmp_path, response):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "Probe Failure.mp3"
    path.write_bytes(b"audio")

    with patch("mammamiradio.playlist.local_library.subprocess.run", return_value=response):
        result = scan_local_library(_config(root))

    assert result.complete is True
    assert len(result.tracks) == 1
    assert result.tracks[0].title == "Probe Failure"


def test_metadata_cache_reprobes_when_file_signature_changes(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "cached.mp3"
    path.write_bytes(b"audio")

    with patch(
        "mammamiradio.playlist.local_library._probe_local_audio_metadata",
        side_effect=[("Artist One", "Title One"), ("Artist Two", "Title Two")],
    ) as probe:
        first = scan_local_library(_config(root)).tracks[0]
        second = scan_local_library(_config(root)).tracks[0]
        path.write_bytes(b"new audio")
        third = scan_local_library(_config(root)).tracks[0]

    assert (first.artist, first.title) == ("Artist One", "Title One")
    assert (second.artist, second.title) == ("Artist One", "Title One")
    assert (third.artist, third.title) == ("Artist Two", "Title Two")
    assert probe.call_count == 2


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


def _probe_response(payload: dict, *, returncode: int = 0):
    return type("ProbeResponse", (), {"returncode": returncode, "stdout": json.dumps(payload)})()


def test_probe_ignores_tags_on_non_audio_streams():
    """Cover art must never name the song.

    An untagged MP3 with embedded artwork carries an attached-picture stream
    whose title is typically "Cover (front)". Accepting it reintroduces exactly
    the wrong-title bug this metadata pass exists to fix.
    """
    response = _probe_response(
        {
            "format": {"tags": {}},
            "streams": [
                {"codec_type": "video", "tags": {"title": "Cover (front)"}},
                {"codec_type": "audio", "tags": {}},
            ],
        }
    )

    with patch("mammamiradio.playlist.local_library.subprocess.run", return_value=response):
        assert _probe_local_audio_metadata(Path("song.mp3")) == ("", "")


def test_probe_rejects_oversized_tag_values():
    """One local file must not be able to hand us an unbounded string."""
    huge = "A" * (local_library_module.LOCAL_METADATA_MAX_TAG_CHARS + 1)
    response = _probe_response({"format": {"tags": {"title": huge, "artist": "Real Artist"}}, "streams": []})

    with patch("mammamiradio.playlist.local_library.subprocess.run", return_value=response):
        artist, title = _probe_local_audio_metadata(Path("song.mp3"))

    assert title == ""
    assert artist == "Real Artist"


def test_probe_refuses_to_parse_oversized_output():
    padding = "B" * local_library_module.LOCAL_METADATA_MAX_PROBE_BYTES
    response = _probe_response({"format": {"tags": {"title": "Real", "comment": padding}}, "streams": []})

    with patch("mammamiradio.playlist.local_library.subprocess.run", return_value=response):
        assert _probe_local_audio_metadata(Path("song.mp3")) == ("", "")


def test_scan_does_not_probe_files_past_the_track_cap(tmp_path, monkeypatch):
    """A library one file over the cap must not re-probe itself forever.

    Probing the overflow file spends ffprobe on audio that is then discarded,
    and its cache entry used to evict a track we keep — so every 60-second scan
    missed on the whole library and probed all of it again.
    """
    monkeypatch.setattr(local_library_module, "MAX_LOCAL_LIBRARY_TRACKS", 3)
    monkeypatch.setattr(local_library_module, "LOCAL_METADATA_CACHE_MAX_ENTRIES", 8)
    monkeypatch.setattr(local_library_module, "_local_metadata_cache", {})
    root = tmp_path / "music"
    root.mkdir()
    for index in range(4):
        (root / f"{index} song.mp3").write_bytes(b"audio")

    probed: list[str] = []

    def _record(path: Path):
        probed.append(path.name)
        return "", ""

    with patch.object(local_library_module, "_probe_local_audio_metadata", side_effect=_record):
        first = scan_local_library(_config(root))
        first_probes = list(probed)
        probed.clear()
        scan_local_library(_config(root))

    assert len(first.tracks) == 3
    assert len(first_probes) == 3, "the file past the cap must never be probed"
    assert probed == [], "a warm library must not be re-probed on the next scan"


def test_scan_stops_probing_once_the_time_budget_is_spent(tmp_path, monkeypatch):
    """asyncio.to_thread() cannot be cancelled, so the scan needs its own deadline."""
    monkeypatch.setattr(local_library_module, "LOCAL_METADATA_SCAN_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(local_library_module, "_local_metadata_cache", {})
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artista - Canzone.mp3").write_bytes(b"audio")

    with patch.object(local_library_module, "_probe_local_audio_metadata", side_effect=AssertionError("probed")):
        result = scan_local_library(_config(root))

    assert result.complete is False
    assert result.tracks[0].artist == "Artista"
    assert result.tracks[0].title == "Canzone"


def test_scan_past_its_budget_still_reuses_already_probed_metadata(tmp_path, monkeypatch):
    """Stopping new probes must not churn a warm library back to filenames."""
    monkeypatch.setattr(local_library_module, "_local_metadata_cache", {})
    root = tmp_path / "music"
    root.mkdir()
    path = root / "Filename Artist - Filename Title.mp3"
    path.write_bytes(b"audio")

    with patch.object(
        local_library_module, "_probe_local_audio_metadata", return_value=("Tagged Artist", "Tagged Title")
    ):
        scan_local_library(_config(root))

    monkeypatch.setattr(local_library_module, "LOCAL_METADATA_SCAN_BUDGET_SECONDS", 0.0)
    with patch.object(local_library_module, "_probe_local_audio_metadata", side_effect=AssertionError("probed")):
        result = scan_local_library(_config(root))

    assert result.tracks[0].artist == "Tagged Artist"
    assert result.tracks[0].title == "Tagged Title"


def test_metadata_probe_is_single_flight_across_concurrent_scans(tmp_path, monkeypatch):
    """Overlapping scans must not each spawn ffprobe for the same file."""
    import threading

    monkeypatch.setattr(local_library_module, "_local_metadata_cache", {})
    monkeypatch.setattr(local_library_module, "_local_metadata_inflight", {})
    root = tmp_path / "music"
    root.mkdir()
    path = root / "shared.mp3"
    path.write_bytes(b"audio")

    started = threading.Event()
    release = threading.Event()
    probes = []

    def _slow_probe(_path: Path):
        probes.append(_path.name)
        started.set()
        release.wait(timeout=5)
        return "Tagged Artist", "Tagged Title"

    results: list[tuple[str, str]] = []

    def _worker():
        results.append(local_library_module._cached_local_audio_metadata(path, path.stat()))

    with patch.object(local_library_module, "_probe_local_audio_metadata", side_effect=_slow_probe):
        first = threading.Thread(target=_worker)
        first.start()
        assert started.wait(timeout=5)
        second = threading.Thread(target=_worker)
        second.start()
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert probes == ["shared.mp3"], "the second scan must reuse the in-flight probe"
    assert results == [("Tagged Artist", "Tagged Title")] * 2


def test_legacy_local_identity_key_reproduces_the_pre_upgrade_pair():
    assert legacy_local_identity_key(Path("/music/29-salvatore-on-everything.mp3")) == (
        "unknown",
        "29-salvatore-on-everything",
    )
    assert legacy_local_identity_key(Path("/music/Artista - Canzone.mp3")) == ("artista", "canzone")


def test_ban_placed_before_the_metadata_upgrade_still_holds():
    """Reading tags changes the key a durable ban was stored under.

    Without the legacy alias the operator's permanent ban silently stops
    matching on the first rescan after the upgrade and the song returns.
    """
    path = Path("/music/29-salvatore-on-everything.mp3")
    track = Track(title="Salvatore On Everything", artist="", duration_ms=0, local_path=path, source="local")
    legacy_ban = {("unknown", "29-salvatore-on-everything"): {"display": "banned"}}

    assert track.normalized_key != legacy_local_identity_key(path)
    assert local_track_is_blocklisted(track, legacy_ban) is True
    assert local_track_is_blocklisted(track, {("unknown", "something-else"): {}}) is False


def test_reconcile_drops_a_track_banned_under_its_legacy_identity(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "29-salvatore-on-everything.mp3"
    path.write_bytes(b"audio")

    with patch("mammamiradio.playlist.local_library._probe_local_audio_metadata", return_value=("", "")):
        scan = scan_local_library(_config(root))

    state = StationState(playlist=[])
    state.blocklist = {("unknown", "29-salvatore-on-everything"): {"display": "banned"}}
    outcome = reconcile_local_library(state, scan)

    assert outcome["banned"] == 1
    assert state.playlist == []


def test_reconcile_carries_a_preference_onto_the_new_identity(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    path = root / "29-salvatore-on-everything.mp3"
    path.write_bytes(b"audio")

    with patch("mammamiradio.playlist.local_library._probe_local_audio_metadata", return_value=("", "")):
        scan = scan_local_library(_config(root))

    state = StationState(playlist=[])
    state.song_preferences = {("unknown", "29-salvatore-on-everything"): {"score": 1}}
    reconcile_local_library(state, scan)

    assert state.song_preferences[("", "salvatore on everything")] == {"score": 1}


def test_a_ban_holds_when_the_probe_was_failing_at_ban_time(tmp_path, monkeypatch):
    """A transient ffprobe failure must not become an escape hatch.

    While a probe is failing the file shows its filename identity, and that is
    the key the operator's ban lands on. When the probe later succeeds the
    identity moves to the tags. Without the filename alias the ban silently
    stops matching and the song returns to rotation.
    """
    monkeypatch.setattr(local_library_module, "_local_metadata_cache", {})
    monkeypatch.setattr(local_library_module, "_local_metadata_inflight", {})
    root = tmp_path / "music"
    root.mkdir()
    path = root / "29-salvatore-on-everything.mp3"
    path.write_bytes(b"audio")

    # Ban placed while the probe was failing: identity is the humanized filename.
    with patch.object(local_library_module, "_probe_local_audio_metadata", return_value=None):
        during_failure = scan_local_library(_config(root)).tracks[0]
    assert during_failure.normalized_key == ("", "salvatore on everything")
    ban = {during_failure.normalized_key: {"display": "Salvatore On Everything"}}

    # The probe recovers and the file's identity moves to its tags.
    with patch.object(
        local_library_module, "_probe_local_audio_metadata", return_value=("Colapesce", "Musica Leggera")
    ):
        after_recovery = scan_local_library(_config(root)).tracks[0]
    assert after_recovery.normalized_key == ("colapesce", "musica leggera")

    assert local_track_is_blocklisted(after_recovery, ban) is True


def test_a_failed_probe_is_not_cached_as_no_tags(tmp_path, monkeypatch):
    """Caching a failure pins the file to its filename for the whole process."""
    monkeypatch.setattr(local_library_module, "_local_metadata_cache", {})
    monkeypatch.setattr(local_library_module, "_local_metadata_inflight", {})
    root = tmp_path / "music"
    root.mkdir()
    (root / "song.mp3").write_bytes(b"audio")

    with patch.object(local_library_module, "_probe_local_audio_metadata", return_value=None):
        scan_local_library(_config(root))
    assert local_library_module._local_metadata_cache == {}, "a failed probe must not be cached"

    with patch.object(local_library_module, "_probe_local_audio_metadata", return_value=("Tagged", "Title")) as probe:
        track = scan_local_library(_config(root)).tracks[0]
    assert probe.call_count == 1, "the failure must be retried on the next pass"
    assert (track.artist, track.title) == ("Tagged", "Title")


def test_a_genuinely_untagged_file_is_cached_and_not_reprobed(tmp_path, monkeypatch):
    """Only FAILURES are retried; a real "no tags" answer is durable."""
    monkeypatch.setattr(local_library_module, "_local_metadata_cache", {})
    monkeypatch.setattr(local_library_module, "_local_metadata_inflight", {})
    root = tmp_path / "music"
    root.mkdir()
    (root / "song.mp3").write_bytes(b"audio")

    with patch.object(local_library_module, "_probe_local_audio_metadata", return_value=("", "")) as probe:
        scan_local_library(_config(root))
        scan_local_library(_config(root))

    assert probe.call_count == 1


def test_clearing_a_migrated_preference_is_not_resurrected_by_the_next_scan(tmp_path):
    """A vote the operator cannot take off is a trap.

    The migration used to COPY the legacy row. Clearing deleted only the new key,
    and the next 60-second scan copied the old one straight back.
    """
    root = tmp_path / "music"
    root.mkdir()
    path = root / "29-salvatore-on-everything.mp3"
    path.write_bytes(b"audio")

    with patch("mammamiradio.playlist.local_library._probe_local_audio_metadata", return_value=("", "")):
        scan = scan_local_library(_config(root))
        state = StationState(playlist=[])
        state.song_preferences = {("unknown", "29-salvatore-on-everything"): {"score": 1}}
        reconcile_local_library(state, scan)

        assert state.song_preferences == {("", "salvatore on everything"): {"score": 1}}

        # Operator clears it, then the scanner runs again.
        state.song_preferences.pop(("", "salvatore on everything"))
        reconcile_local_library(state, scan_local_library(_config(root)))

    assert state.song_preferences == {}

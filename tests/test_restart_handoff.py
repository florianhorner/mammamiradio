from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from mammamiradio.core.models import Segment, SegmentType
from mammamiradio.restart_handoff import (
    RestartHandoffCandidate,
    RestartHandoffEntry,
    RestartHandoffManifest,
    admit_restart_handoff_entries,
    admit_restart_handoff_manifest,
    load_restart_handoff_manifest,
    prune_stale_handoff_tmp_files,
    restart_handoff_dir,
    restart_handoff_manifest_path,
    try_write_restart_handoff_spool,
    write_restart_handoff_spool,
)


def _duration(_path: Path) -> float:
    return 181.0


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_cache_file(cache_dir: Path, name: str = "norm_artist_song_192k.mp3", data: bytes = b"audio") -> Path:
    path = cache_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _write_spooled_file(cache_dir: Path, name: str = "song.mp3", data: bytes = b"audio") -> Path:
    path = restart_handoff_dir(cache_dir) / "segments" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _write_manifest(cache_dir: Path, manifest: RestartHandoffManifest) -> None:
    path = restart_handoff_manifest_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")


def _entry_for_path(
    cache_dir: Path,
    path: Path,
    *,
    created_at: float = 100.0,
    duration_sec: float = 181.0,
    artist: str = "Artist",
    title: str = "Song",
    segment_class: str = "music",
    metadata: dict | None = None,
) -> RestartHandoffEntry:
    return RestartHandoffEntry(
        relative_path=path.relative_to(restart_handoff_dir(cache_dir)).as_posix(),
        sha256=_sha(path),
        size_bytes=path.stat().st_size,
        duration_sec=duration_sec,
        artist=artist,
        title=title,
        segment_class=segment_class,
        created_at=created_at,
        source_path=str(path),
        metadata=metadata or {},
    )


def test_manifest_load_is_tolerant_for_missing_corrupt_and_wrong_schema(tmp_path):
    assert load_restart_handoff_manifest(tmp_path).entries == ()

    manifest_path = restart_handoff_manifest_path(tmp_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("{not json", encoding="utf-8")
    assert load_restart_handoff_manifest(tmp_path).entries == ()

    manifest_path.write_text(json.dumps({"schema_version": 999, "entries": [{"not": "ours"}]}), encoding="utf-8")
    assert load_restart_handoff_manifest(tmp_path).entries == ()


def test_write_spool_copies_hashes_publishes_manifest_and_admits(tmp_path):
    source = _write_cache_file(tmp_path, data=b"durable music bytes")
    candidate = RestartHandoffCandidate(
        path=source,
        duration_sec=180.0,
        artist="Artist",
        title="Song",
        metadata={"audio_source": "download", "source_kind": "charts"},
        ephemeral=False,
    )

    manifest = write_restart_handoff_spool(tmp_path, [candidate], now=100.0, duration_probe=_duration)

    assert len(manifest.entries) == 1
    entry = manifest.entries[0]
    spooled_path = entry.path(tmp_path)
    assert spooled_path is not None
    assert spooled_path.exists()
    assert spooled_path.read_bytes() == b"durable music bytes"
    assert entry.sha256 == hashlib.sha256(b"durable music bytes").hexdigest()
    assert entry.relative_path.startswith("segments/")
    assert restart_handoff_manifest_path(tmp_path).exists()
    assert not [p for p in restart_handoff_dir(tmp_path).rglob("*") if p.name.startswith((".handoff-", ".manifest-"))]

    admission = admit_restart_handoff_entries(tmp_path, now=120.0, duration_probe=_duration)
    assert admission.rejected == ()
    assert admission.accepted == (entry,)
    [segment] = admission.to_segments(tmp_path)
    assert segment.type is SegmentType.MUSIC
    assert segment.path == spooled_path
    assert segment.ephemeral is False
    assert segment.metadata["audio_source"] == "restart_handoff"
    assert segment.metadata["title_only"] == "Song"


def test_prune_stale_handoff_tmp_files_removes_only_old_scratch_files(tmp_path):
    handoff_dir = restart_handoff_dir(tmp_path)
    segments_dir = handoff_dir / "segments"
    segments_dir.mkdir(parents=True)
    old_mtime = time.time() - 7 * 3600

    old_manifest_tmp = handoff_dir / ".manifest-old.tmp"
    old_handoff_tmp = segments_dir / ".handoff-old.tmp"
    recent_manifest_tmp = handoff_dir / ".manifest-recent.tmp"
    recent_handoff_tmp = segments_dir / ".handoff-recent.tmp"
    final_manifest = handoff_dir / "manifest.json"
    final_segment = segments_dir / "abc123.mp3"
    unrelated_tmp = handoff_dir / "handoff-old.tmp"
    for path in (
        old_manifest_tmp,
        old_handoff_tmp,
        recent_manifest_tmp,
        recent_handoff_tmp,
        final_manifest,
        final_segment,
        unrelated_tmp,
    ):
        path.write_bytes(b"data")
    for path in (old_manifest_tmp, old_handoff_tmp, final_manifest, final_segment, unrelated_tmp):
        os.utime(path, (old_mtime, old_mtime))

    assert prune_stale_handoff_tmp_files(tmp_path, max_age_hours=6) == 2

    assert not old_manifest_tmp.exists()
    assert not old_handoff_tmp.exists()
    assert recent_manifest_tmp.exists()
    assert recent_handoff_tmp.exists()
    assert final_manifest.exists()
    assert final_segment.exists()
    assert unrelated_tmp.exists()


def test_prune_stale_handoff_tmp_files_tolerates_missing_dirs(tmp_path):
    assert prune_stale_handoff_tmp_files(tmp_path) == 0

    restart_handoff_dir(tmp_path).mkdir(parents=True)
    assert prune_stale_handoff_tmp_files(tmp_path) == 0


def test_prune_stale_handoff_tmp_files_logs_and_continues_on_oserror(tmp_path, caplog):
    handoff_dir = restart_handoff_dir(tmp_path)
    segments_dir = handoff_dir / "segments"
    segments_dir.mkdir(parents=True)
    old_mtime = time.time() - 7 * 3600
    good = handoff_dir / ".manifest-good.tmp"
    bad = segments_dir / ".handoff-bad.tmp"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")
    os.utime(good, (old_mtime, old_mtime))
    os.utime(bad, (old_mtime, old_mtime))

    original_unlink = Path.unlink

    def _unlink(path: Path, missing_ok: bool = False) -> None:
        if path == bad:
            raise OSError("permission denied")
        original_unlink(path, missing_ok=missing_ok)

    with patch.object(Path, "unlink", autospec=True, side_effect=_unlink):
        assert prune_stale_handoff_tmp_files(tmp_path, max_age_hours=6) == 1

    assert "Failed to prune restart handoff scratch file" in caplog.text
    assert "permission denied" in caplog.text


def test_write_spool_preserves_existing_manifest_when_no_candidates_are_accepted(tmp_path):
    existing_path = _write_spooled_file(tmp_path, "existing.mp3", b"existing music")
    existing_entry = _entry_for_path(tmp_path, existing_path, created_at=50.0, artist="Existing", title="Song")
    existing_manifest = RestartHandoffManifest(entries=(existing_entry,), created_at=50.0)
    _write_manifest(tmp_path, existing_manifest)
    rejected_source = _write_cache_file(tmp_path, "norm_rejected_192k.mp3", b"new music")
    rejected_candidate = RestartHandoffCandidate(
        rejected_source,
        180.0,
        "Rejected",
        "Song",
        ephemeral=True,
    )

    manifest = write_restart_handoff_spool(
        tmp_path,
        [rejected_candidate],
        now=100.0,
        duration_probe=_duration,
    )

    assert manifest == existing_manifest
    assert load_restart_handoff_manifest(tmp_path) == existing_manifest
    assert existing_path.exists()


def test_write_spool_can_explicitly_clear_manifest_when_no_candidates_are_accepted(tmp_path):
    existing_path = _write_spooled_file(tmp_path, "existing.mp3", b"existing music")
    existing_entry = _entry_for_path(tmp_path, existing_path, created_at=50.0, artist="Existing", title="Song")
    _write_manifest(tmp_path, RestartHandoffManifest(entries=(existing_entry,), created_at=50.0))
    rejected_source = _write_cache_file(tmp_path, "norm_rejected_192k.mp3", b"new music")
    rejected_candidate = RestartHandoffCandidate(
        rejected_source,
        180.0,
        "Rejected",
        "Song",
        ephemeral=True,
    )

    manifest = write_restart_handoff_spool(
        tmp_path,
        [rejected_candidate],
        now=100.0,
        duration_probe=_duration,
        clear_when_empty=True,
    )

    assert manifest.entries == ()
    assert load_restart_handoff_manifest(tmp_path).entries == ()
    assert not existing_path.exists()


def test_write_spool_prunes_unreferenced_segments_after_successful_publish(tmp_path):
    stale_path = _write_spooled_file(tmp_path, "stale.mp3", b"stale music")
    source = _write_cache_file(tmp_path, "norm_new_192k.mp3", b"fresh music")
    candidate = RestartHandoffCandidate(
        path=source,
        duration_sec=180.0,
        artist="Artist",
        title="Fresh",
        metadata={"audio_source": "download"},
        ephemeral=False,
    )

    manifest = write_restart_handoff_spool(tmp_path, [candidate], now=100.0, duration_probe=_duration)

    [entry] = manifest.entries
    spooled_path = entry.path(tmp_path)
    assert spooled_path is not None
    assert spooled_path.exists()
    assert not stale_path.exists()


def test_write_spool_protects_queued_admitted_files_but_still_prunes_others(tmp_path):
    """F2: a handoff file still referenced by the live queue must survive the
    single-candidate spool rewrite + prune (else it is deleted out from under the
    playback loop -> cold-open dead air). Unprotected stale files still go."""
    protected_path = _write_spooled_file(tmp_path, "queued_admitted.mp3", b"still queued")
    stale_path = _write_spooled_file(tmp_path, "stale.mp3", b"stale music")
    source = _write_cache_file(tmp_path, "norm_new_192k.mp3", b"fresh music")
    candidate = RestartHandoffCandidate(
        path=source,
        duration_sec=180.0,
        artist="Artist",
        title="Fresh",
        metadata={"audio_source": "download"},
        ephemeral=False,
    )

    write_restart_handoff_spool(
        tmp_path,
        [candidate],
        now=100.0,
        duration_probe=_duration,
        protected_paths=[protected_path],
    )

    assert protected_path.exists()  # still-queued admitted file survives the prune
    assert not stale_path.exists()  # unprotected stale file is still pruned


def test_candidate_from_segment_uses_music_class_and_title_identity(tmp_path):
    path = _write_cache_file(tmp_path)
    segment = Segment(
        type=SegmentType.MUSIC,
        path=path,
        duration_sec=180.0,
        metadata={"artist": "Artist", "title": "Artist – Song"},
        ephemeral=False,
    )

    candidate = RestartHandoffCandidate.from_segment(segment)

    assert candidate.segment_class == "music"
    assert candidate.artist == "Artist"
    assert candidate.title == "Song"


def test_write_spool_skips_ephemeral_dynamic_temp_outside_and_non_music_candidates(tmp_path):
    cache_dir = tmp_path / "cache"
    good = _write_cache_file(cache_dir, "norm_good_192k.mp3", b"good")
    temp = _write_cache_file(cache_dir, ".handoff-temp.mp3", b"temp")
    outside = _write_cache_file(tmp_path / "outside", "norm_outside_192k.mp3", b"outside")

    candidates = [
        RestartHandoffCandidate(good, 180.0, "Artist", "Ephemeral", ephemeral=True),
        RestartHandoffCandidate(good, 180.0, "Artist", "Overlay", metadata={"dynamic_overlay": True}),
        RestartHandoffCandidate(temp, 180.0, "Artist", "Temp"),
        RestartHandoffCandidate(outside, 180.0, "Artist", "Outside"),
        RestartHandoffCandidate(good, 180.0, "Artist", "Talk", segment_class="voice"),
        RestartHandoffCandidate(good, 180.0, "Artist", "Good"),
    ]

    manifest = write_restart_handoff_spool(cache_dir, candidates, now=100.0, duration_probe=_duration)

    assert [entry.title for entry in manifest.entries] == ["Good"]


def test_try_write_spool_logs_and_swallows_failures(tmp_path, caplog):
    source = _write_cache_file(tmp_path)
    candidate = RestartHandoffCandidate(source, 180.0, "Artist", "Song")

    with patch("mammamiradio.restart_handoff._publish_manifest", side_effect=OSError("disk full")):
        assert try_write_restart_handoff_spool(tmp_path, [candidate], duration_probe=_duration) is False

    assert "Failed to write restart handoff spool" in caplog.text


def test_admission_rejects_hash_mismatch(tmp_path):
    path = _write_spooled_file(tmp_path, data=b"original")
    entry = _entry_for_path(tmp_path, path)
    path.write_bytes(b"mutated!")

    admission = admit_restart_handoff_manifest(
        tmp_path, RestartHandoffManifest(entries=(entry,), created_at=100.0), now=120.0, duration_probe=_duration
    )

    assert path.stat().st_size == entry.size_bytes
    assert [rejection.reason for rejection in admission.rejected] == ["hash_mismatch"]
    assert admission.accepted == ()


def test_admission_rejects_when_hash_read_races_with_deletion(tmp_path):
    """The file passes stat() but vanishes before the hash read (concurrent
    prune, disk hiccup) — _sha256_file must be caught, not propagate into the
    startup admission path."""
    path = _write_spooled_file(tmp_path, data=b"original")
    entry = _entry_for_path(tmp_path, path)

    with patch("mammamiradio.restart_handoff._sha256_file", side_effect=OSError("vanished mid-read")):
        admission = admit_restart_handoff_manifest(
            tmp_path, RestartHandoffManifest(entries=(entry,), created_at=100.0), now=120.0, duration_probe=_duration
        )

    assert [rejection.reason for rejection in admission.rejected] == ["missing_file"]
    assert admission.accepted == ()


def test_admission_rejects_size_mismatch(tmp_path):
    path = _write_spooled_file(tmp_path, data=b"original-bytes")
    entry = _entry_for_path(tmp_path, path)
    path.write_bytes(b"a-shorter-different-length-payload-now")

    admission = admit_restart_handoff_manifest(
        tmp_path, RestartHandoffManifest(entries=(entry,), created_at=100.0), now=120.0, duration_probe=_duration
    )

    assert [rejection.reason for rejection in admission.rejected] == ["size_mismatch"]
    assert admission.accepted == ()


def test_admission_rejects_invalid_created_at(tmp_path):
    path = _write_spooled_file(tmp_path)
    entry = _entry_for_path(tmp_path, path, created_at=-5.0)

    admission = admit_restart_handoff_manifest(
        tmp_path, RestartHandoffManifest(entries=(entry,), created_at=100.0), now=120.0, duration_probe=_duration
    )

    assert [rejection.reason for rejection in admission.rejected] == ["invalid_created_at"]
    assert admission.accepted == ()


def test_admission_rejects_missing_file(tmp_path):
    path = _write_spooled_file(tmp_path)
    entry = _entry_for_path(tmp_path, path)
    path.unlink()

    admission = admit_restart_handoff_manifest(
        tmp_path, RestartHandoffManifest(entries=(entry,), created_at=100.0), now=120.0, duration_probe=_duration
    )

    assert [rejection.reason for rejection in admission.rejected] == ["missing_file"]


def test_admission_rejects_invalid_duration_from_manifest_or_probe(tmp_path):
    first = _write_spooled_file(tmp_path, "first.mp3", b"first")
    zero_manifest = _entry_for_path(tmp_path, first, duration_sec=0.0)
    second = _write_spooled_file(tmp_path, "second.mp3", b"second")
    bad_probe = _entry_for_path(tmp_path, second, duration_sec=180.0)

    zero_admission = admit_restart_handoff_manifest(
        tmp_path,
        RestartHandoffManifest(entries=(zero_manifest,), created_at=100.0),
        now=120.0,
        duration_probe=_duration,
    )
    probe_admission = admit_restart_handoff_manifest(
        tmp_path,
        RestartHandoffManifest(entries=(bad_probe,), created_at=100.0),
        now=120.0,
        duration_probe=lambda _path: None,
    )

    assert [rejection.reason for rejection in zero_admission.rejected] == ["invalid_duration"]
    assert [rejection.reason for rejection in probe_admission.rejected] == ["invalid_duration"]


def test_admission_rejects_too_old_entry(tmp_path):
    path = _write_spooled_file(tmp_path)
    entry = _entry_for_path(tmp_path, path, created_at=10.0)

    admission = admit_restart_handoff_manifest(
        tmp_path,
        RestartHandoffManifest(entries=(entry,), created_at=10.0),
        now=100.0,
        max_age_sec=30.0,
        duration_probe=_duration,
    )

    assert [rejection.reason for rejection in admission.rejected] == ["too_old"]


def test_admission_rejects_too_many_segments(tmp_path):
    entries = tuple(
        _entry_for_path(tmp_path, _write_spooled_file(tmp_path, f"{idx}.mp3", f"{idx}".encode())) for idx in range(4)
    )

    admission = admit_restart_handoff_manifest(
        tmp_path,
        RestartHandoffManifest(entries=entries, created_at=100.0),
        now=120.0,
        max_entries=3,
        duration_probe=_duration,
    )

    assert admission.accepted == ()
    assert [rejection.reason for rejection in admission.rejected] == ["too_many_segments"]


def test_admission_rejects_blocklisted_artist_title(tmp_path):
    path = _write_spooled_file(tmp_path)
    entry = _entry_for_path(tmp_path, path, artist="Artist", title="Song")

    admission = admit_restart_handoff_manifest(
        tmp_path,
        RestartHandoffManifest(entries=(entry,), created_at=100.0),
        blocklist={("artist", "song"): {"display": "Artist - Song"}},
        now=120.0,
        duration_probe=_duration,
    )

    assert [rejection.reason for rejection in admission.rejected] == ["blocklisted"]


def test_admission_rejects_non_music_and_ephemeral_overlay_markers(tmp_path):
    voice_path = _write_spooled_file(tmp_path, "voice.mp3", b"voice")
    overlay_path = _write_spooled_file(tmp_path, "overlay.mp3", b"overlay")
    voice = _entry_for_path(tmp_path, voice_path, segment_class="voice")
    overlay = _entry_for_path(tmp_path, overlay_path, metadata={"rescue": True})

    admission = admit_restart_handoff_manifest(
        tmp_path,
        RestartHandoffManifest(entries=(voice, overlay), created_at=100.0),
        now=120.0,
        duration_probe=_duration,
    )

    assert [rejection.reason for rejection in admission.rejected] == [
        "non_music_segment_class",
        "ephemeral_or_dynamic_marker",
    ]


def test_admission_rejects_absolute_traversal_and_temp_paths(tmp_path):
    real = _write_spooled_file(tmp_path, "real.mp3", b"real")
    absolute = _entry_for_path(tmp_path, real)
    absolute = RestartHandoffEntry(**{**absolute.to_dict(), "relative_path": str(real)})
    traversal = _entry_for_path(tmp_path, real)
    traversal = RestartHandoffEntry(**{**traversal.to_dict(), "relative_path": "../real.mp3"})
    temp = _write_spooled_file(tmp_path, ".partial.tmp.mp3", b"tmp")
    temp_entry = _entry_for_path(tmp_path, temp)

    admission = admit_restart_handoff_manifest(
        tmp_path,
        RestartHandoffManifest(entries=(absolute, traversal, temp_entry), created_at=100.0),
        now=120.0,
        duration_probe=_duration,
    )

    assert [rejection.reason for rejection in admission.rejected] == [
        "invalid_path",
        "invalid_path",
        "temporary_path",
    ]

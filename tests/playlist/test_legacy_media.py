from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import mammamiradio.playlist.legacy_media as legacy_media
from mammamiradio.audio.norm_cache import norm_cache_origin_is_eligible, select_norm_cache_rescue
from mammamiradio.audio.normalizer import save_track_metadata
from mammamiradio.core.models import StationState
from mammamiradio.core.sync import init_db
from mammamiradio.playlist.legacy_media import (
    reconcile_legacy_external_media,
    reconciliation_marker_path,
)
from mammamiradio.restart_handoff import (
    RestartHandoffEntry,
    RestartHandoffManifest,
    admit_restart_handoff_manifest,
)


def _insert_external_db_state(db_path: Path, cached: Path) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "INSERT INTO tracks (youtube_id, title, artist, duration_s, file_path) VALUES (?, ?, ?, ?, ?)",
        ("dQw4w9WgXcQ", "External", "Artist", 180.0, str(cached)),
    )
    assert cursor.lastrowid is not None
    track_id = cursor.lastrowid
    conn.execute("INSERT INTO play_history (track_id) VALUES (?)", (track_id,))
    conn.execute("INSERT INTO track_rules (youtube_id, rule_text) VALUES (?, ?)", ("dQw4w9WgXcQ", "rule"))
    conn.execute(
        "INSERT INTO song_cues (youtube_id, cue_type, cue_text) VALUES (?, ?, ?)",
        ("dQw4w9WgXcQ", "anthem", "cue"),
    )
    conn.execute(
        "INSERT INTO tracks (youtube_id, title, artist, duration_s, file_path) VALUES (?, ?, ?, ?, ?)",
        ("not-a-video-id", "Unknown", "Artist", 180.0, "unknown.mp3"),
    )
    conn.commit()
    conn.close()


def _handoff_entry(path: Path, *, source_kind: str) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "relative_path": f"segments/{path.name}",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "duration_sec": 180.0,
        "artist": "Artist",
        "title": path.stem,
        "segment_class": "music",
        "created_at": 100.0,
        "source_path": "",
        "metadata": {"source_kind": source_kind},
    }


def test_reconciliation_removes_only_exact_external_state(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    db_path = cache_dir / "mammamiradio.db"
    init_db(db_path)

    recognized = [
        cache_dir / "youtube_dqw4w9wgxcq.mp3",
        cache_dir / "youtube_dqw4w9wgxcq.m4a",
        cache_dir / "youtube_dqw4w9wgxcq.part",
        cache_dir / "youtube_dqw4w9wgxcq.tmp",
        cache_dir / "youtube_dqw4w9wgxcq.webm",
        cache_dir / "youtube_dqw4w9wgxcq.mp3.json",
        cache_dir / "youtube_dqw4w9wgxcq.0123456789abcdef0123456789abcdef.tmp",
        cache_dir / "norm_youtube_dqw4w9wgxcq_192k.mp3",
        cache_dir / "norm_youtube_dqw4w9wgxcq_192k.mp3.json",
        cache_dir / "jamendo_jamendo_12345.mp3",
        cache_dir / "fm_norm_jamendo_jamendo_12345_1_clean.mp3",
        cache_dir / "_failed_jamendo_jamendo_12345.mp3",
        cache_dir / "artist_title_youtube.mp3",
    ]
    for path in recognized:
        path.write_bytes(b"external")
    unknown = [
        cache_dir / "norm_unknown_legacy_192k.mp3",
        cache_dir / "youtube_too-short.mp3",
        cache_dir / "norm_youtube_dqw4w9wgxcq_0k.mp3",
        cache_dir / "jamendo_jamendo_not-numeric.mp3",
        cache_dir / "operator_youtube_notes.txt",
    ]
    for path in unknown:
        path.write_bytes(b"unknown")
    operator_music = cache_dir / "music" / "Artist - Mine.mp3"
    operator_music.parent.mkdir()
    operator_music.write_bytes(b"operator")
    ytdlp_tmp = cache_dir / ".ytdlp_tmp" / "job"
    ytdlp_tmp.mkdir(parents=True)
    (ytdlp_tmp / "fragment.part").write_bytes(b"fragment")

    db_cached = cache_dir / "dQw4w9WgXcQ.mp3"
    db_cached.write_bytes(b"synced")
    _insert_external_db_state(db_path, db_cached)

    handoff_root = cache_dir / "restart_handoff"
    segments = handoff_root / "segments"
    segments.mkdir(parents=True)
    external_handoff = segments / "external.mp3"
    local_handoff = segments / "local.mp3"
    external_handoff.write_bytes(b"external handoff")
    local_handoff.write_bytes(b"local handoff")
    manifest = {
        "schema_version": 1,
        "created_at": 100.0,
        "entries": [
            _handoff_entry(external_handoff, source_kind="youtube"),
            _handoff_entry(local_handoff, source_kind="local"),
        ],
    }
    (handoff_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = reconcile_legacy_external_media(cache_dir, db_path)

    assert result.already_complete is False
    assert result.removed_handoff_entries == 1
    assert result.removed_db_rows == 4
    assert all(not path.exists() for path in recognized)
    assert not db_cached.exists()
    assert not external_handoff.exists()
    assert not (cache_dir / ".ytdlp_tmp").exists()
    assert all(path.read_bytes() == b"unknown" for path in unknown)
    assert operator_music.read_bytes() == b"operator"
    assert local_handoff.read_bytes() == b"local handoff"
    remaining_manifest = json.loads((handoff_root / "manifest.json").read_text(encoding="utf-8"))
    assert [entry["metadata"]["source_kind"] for entry in remaining_manifest["entries"]] == ["local"]

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT youtube_id FROM tracks").fetchall() == [("not-a-video-id",)]
    assert conn.execute("SELECT COUNT(*) FROM play_history").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM track_rules").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM song_cues").fetchone() == (0,)
    conn.close()
    assert reconciliation_marker_path(cache_dir).exists()

    later = cache_dir / "jamendo_jamendo_999.mp3"
    later.write_bytes(b"later")
    repeated = reconcile_legacy_external_media(cache_dir, db_path)
    assert repeated.already_complete is True
    assert later.exists()


def test_interrupted_reconciliation_retries_without_fabricating_marker(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    db_path = cache_dir / "mammamiradio.db"
    init_db(db_path)
    artifact = cache_dir / "youtube_dqw4w9wgxcq.mp3"
    artifact.write_bytes(b"external")

    with (
        patch("mammamiradio.playlist.legacy_media._publish_marker", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        reconcile_legacy_external_media(cache_dir, db_path)

    assert not artifact.exists()
    assert not reconciliation_marker_path(cache_dir).exists()

    result = reconcile_legacy_external_media(cache_dir, db_path)
    assert result.already_complete is False
    assert reconciliation_marker_path(cache_dir).exists()


def test_reconciliation_is_safe_with_no_prior_cache_or_database(tmp_path):
    cache_dir = tmp_path / "cache"
    db_path = tmp_path / "missing.db"

    result = reconcile_legacy_external_media(cache_dir, db_path)

    assert result == legacy_media.LegacyMediaReconciliation()
    assert not db_path.exists()
    assert json.loads(reconciliation_marker_path(cache_dir).read_text(encoding="utf-8")) == {
        "schema_version": legacy_media.RECONCILIATION_VERSION
    }


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema_version": 2, "entries": []},
        {"schema_version": 1, "entries": {}},
        {
            "schema_version": 1,
            "entries": [
                None,
                {"metadata": "not-a-dict"},
                {"metadata": {"source_kind": "local"}},
            ],
        },
    ],
)
def test_handoff_cleanup_leaves_unknown_manifest_shapes_untouched(tmp_path, payload):
    manifest = tmp_path / "restart_handoff" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    original = json.dumps(payload)
    manifest.write_text(original, encoding="utf-8")

    assert legacy_media._remove_external_handoff_entries(tmp_path) == (0, 0)
    assert manifest.read_text(encoding="utf-8") == original


def test_handoff_cleanup_leaves_unreadable_manifest_untouched(tmp_path, caplog):
    manifest = tmp_path / "restart_handoff" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    original = b"\xff\xfe"
    manifest.write_bytes(original)

    assert legacy_media._remove_external_handoff_entries(tmp_path) == (0, 0)
    assert manifest.read_bytes() == original
    assert "left an unreadable handoff manifest untouched" in caplog.text


def test_handoff_cleanup_recognizes_exact_metadata_and_rejects_unsafe_paths(tmp_path):
    handoff_root = tmp_path / "restart_handoff"
    segments = handoff_root / "segments"
    segments.mkdir(parents=True)
    chart = segments / "chart.mp3"
    classic = segments / "classic.mp3"
    local = segments / "local.mp3"
    outside = tmp_path / "outside.mp3"
    for path in (chart, classic, local, outside):
        path.write_bytes(path.name.encode())

    entries = [
        {"relative_path": "segments/chart.mp3", "metadata": {"source": "charts"}},
        {"relative_path": "segments/classic.mp3", "metadata": {"audio_source": "classic"}},
        {"relative_path": 123, "metadata": {"source_kind": "ytdlp"}},
        {"relative_path": "../outside.mp3", "metadata": {"provider": "JAMENDO"}},
        {"relative_path": "segments/already-missing.mp3", "metadata": {"youtube_id": "dQw4w9WgXcQ"}},
        {"relative_path": "segments/local.mp3", "metadata": {"source_kind": "local"}},
    ]
    manifest = handoff_root / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8")

    removed_entries, removed_files = legacy_media._remove_external_handoff_entries(tmp_path)

    assert (removed_entries, removed_files) == (5, 2)
    assert not chart.exists()
    assert not classic.exists()
    assert local.exists()
    assert outside.exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["entries"] == [entries[-1]]


def test_cache_cleanup_preserves_symlinked_ytdlp_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    fragment = outside / "fragment.part"
    fragment.write_bytes(b"outside")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / ".ytdlp_tmp").symlink_to(outside, target_is_directory=True)

    assert legacy_media._remove_recognized_cache_files(cache_dir) == 0
    assert fragment.read_bytes() == b"outside"


def test_cache_cleanup_skips_ytdlp_directory_when_containment_is_uncertain(tmp_path):
    cache_dir = tmp_path / "cache"
    ytdlp_tmp = cache_dir / ".ytdlp_tmp"
    ytdlp_tmp.mkdir(parents=True)
    fragment = ytdlp_tmp / "fragment.part"
    fragment.write_bytes(b"keep")

    with patch("mammamiradio.playlist.legacy_media.safe_path_within", return_value=None):
        assert legacy_media._remove_recognized_cache_files(cache_dir) == 0

    assert fragment.read_bytes() == b"keep"


def test_db_cleanup_handles_partial_schema_and_only_cache_owned_files(tmp_path):
    cache_dir = tmp_path / "cache"
    operator_music = cache_dir / "music" / "mine.mp3"
    relative_cache = cache_dir / "relative.mp3"
    outside = tmp_path / "outside.mp3"
    operator_music.parent.mkdir(parents=True)
    for path in (operator_music, relative_cache, outside):
        path.write_bytes(path.name.encode())

    db_path = tmp_path / "partial.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, youtube_id TEXT, file_path TEXT)")
    rows = [
        ("dQw4w9WgXcQ", ""),
        ("abcdefghijk", str(operator_music)),
        ("ZYXWVUTSRQP", "relative.mp3"),
        ("12345678901", str(outside)),
        ("98765432109", "missing.mp3"),
        ("not-external", "unknown.mp3"),
    ]
    conn.executemany("INSERT INTO tracks (youtube_id, file_path) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()

    removed_rows, removed_files = legacy_media._remove_external_db_rows(cache_dir, db_path)

    assert (removed_rows, removed_files) == (5, 1)
    assert operator_music.exists()
    assert not relative_cache.exists()
    assert outside.exists()
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT youtube_id FROM tracks").fetchall() == [("not-external",)]
    conn.close()
    assert legacy_media._is_operator_music_path(outside, cache_dir) is False
    assert legacy_media._remove_file(outside, cache_dir) == 0


def test_db_cleanup_ignores_database_without_tracks_table(tmp_path):
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    conn.close()

    assert legacy_media._remove_external_db_rows(tmp_path, db_path) == (0, 0)


def test_remove_file_tolerates_a_disappearing_cache_artifact(tmp_path):
    artifact = tmp_path / "youtube_dqw4w9wgxcq.mp3"
    artifact.write_bytes(b"external")

    with patch.object(Path, "unlink", side_effect=FileNotFoundError):
        assert legacy_media._remove_file(artifact, tmp_path) == 0


def test_marker_publish_tolerates_permission_hardening_failure(tmp_path):
    marker = tmp_path / "state" / "marker.json"

    with patch.object(Path, "chmod", side_effect=OSError("chmod unavailable")):
        legacy_media._publish_marker(marker)

    assert json.loads(marker.read_text(encoding="utf-8")) == {"schema_version": legacy_media.RECONCILIATION_VERSION}


def test_atomic_json_write_removes_staging_file_after_replace_failure(tmp_path):
    target = tmp_path / "state.json"

    with (
        patch("mammamiradio.playlist.legacy_media.os.replace", side_effect=OSError("replace failed")),
        pytest.raises(OSError, match="replace failed"),
    ):
        legacy_media._atomic_write_json(target, {"value": 1})

    assert not target.exists()
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_atomic_json_write_preserves_original_error_when_staging_cleanup_fails(tmp_path):
    target = tmp_path / "state.json"

    with (
        patch("mammamiradio.playlist.legacy_media.os.replace", side_effect=OSError("replace failed")),
        patch("mammamiradio.playlist.legacy_media.os.unlink", side_effect=OSError("unlink failed")),
        pytest.raises(OSError, match="replace failed"),
    ):
        legacy_media._atomic_write_json(target, {"value": 1})

    assert not target.exists()
    for staging in tmp_path.glob(".state.json.*.tmp"):
        staging.unlink()


def test_strict_norm_cache_gate_preserves_unknown_but_never_selects_it(tmp_path):
    unknown = tmp_path / "norm_unknown_legacy_192k.mp3"
    unknown.write_bytes(b"unknown")
    (tmp_path / "norm_unknown_legacy_192k.mp3.json").write_text(
        json.dumps({"title": "Unknown", "artist": "Unknown"}),
        encoding="utf-8",
    )
    local = tmp_path / "norm_local_track_192k.mp3"
    local.write_bytes(b"local")
    save_track_metadata(local, "Local", "Operator", source_kind="local")
    youtube = tmp_path / "norm_youtube_dqw4w9wgxcq_192k.mp3"
    youtube.write_bytes(b"youtube")
    save_track_metadata(youtube, "External", "Artist", source_kind="youtube")
    starter = tmp_path / "norm_starter_carefree_192k.mp3"
    starter.write_bytes(b"forged starter")
    save_track_metadata(starter, "Carefree", "Kevin MacLeod", source_kind="starter")

    assert norm_cache_origin_is_eligible(unknown) is False
    assert norm_cache_origin_is_eligible(local) is True
    assert norm_cache_origin_is_eligible(youtube) is False
    assert norm_cache_origin_is_eligible(youtube, allow_external_media=True) is True
    assert norm_cache_origin_is_eligible(starter) is False
    assert (
        select_norm_cache_rescue(
            tmp_path,
            StationState(),
            require_known_origin=True,
            allow_external_media=False,
        )
        == local
    )
    assert unknown.exists()
    assert youtube.exists()
    assert starter.exists()


def test_strict_handoff_gate_rejects_unknown_jamendo_and_disabled_external(tmp_path):
    handoff_root = tmp_path / "restart_handoff"
    segments = handoff_root / "segments"
    segments.mkdir(parents=True)

    def entry(name: str, source_kind: str | None) -> RestartHandoffEntry:
        path = segments / f"{name}.mp3"
        path.write_bytes(name.encode())
        metadata = {} if source_kind is None else {"source_kind": source_kind}
        return RestartHandoffEntry(
            relative_path=f"segments/{path.name}",
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            size_bytes=path.stat().st_size,
            duration_sec=180.0,
            artist="Artist",
            title=name,
            created_at=100.0,
            metadata=metadata,
        )

    local = entry("local", "local")
    unknown = entry("unknown", None)
    jamendo = entry("jamendo", "jamendo")
    starter = entry("starter", "starter")
    youtube = entry("youtube", "youtube")
    admission = admit_restart_handoff_manifest(
        tmp_path,
        RestartHandoffManifest(entries=(local, unknown, jamendo, starter, youtube), created_at=100.0),
        now=120.0,
        max_entries=5,
        duration_probe=lambda _path: 180.0,
        allow_external_media=False,
        require_known_source=True,
    )

    assert admission.accepted == (local,)
    assert [rejected.reason for rejected in admission.rejected] == [
        "unknown_source",
        "persistent_jamendo_retired",
        "starter_direct_only",
        "external_media_disabled",
    ]

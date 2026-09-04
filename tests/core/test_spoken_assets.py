"""Packaged speech must be reviewed, content-addressed, and truth-safe."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mammamiradio.core import spoken_assets
from mammamiradio.core.spoken_assets import (
    approved_spoken_asset_entries,
    approved_spoken_assets,
    is_approved_packaged_audio_asset,
    is_approved_spoken_asset,
    validate_spoken_asset_manifest,
)


def _write_manifest(root, entries):
    (root / "spoken_assets.json").write_text(
        json.dumps({"schema_version": 1, "assets": entries}),
        encoding="utf-8",
    )


def _entry(path, payload, *, transcript="The station stays on air.", kind="speech", language="en"):
    return {
        "path": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "kind": kind,
        "language": language,
        "transcript": transcript,
    }


def test_shipped_manifest_is_valid_and_declares_reviewed_spoken_assets():
    assert validate_spoken_asset_manifest() == []
    recovery = approved_spoken_assets("recovery")
    first_listen = approved_spoken_assets("first_listen")
    banter = approved_spoken_asset_entries("banter")
    assert [path.name for path in recovery] == ["continuity_1.mp3"]
    assert [path.name for path in first_listen] == ["first_listen_show.mp3"]
    assert len(banter) == 21
    assert sum(entry.mode == "normal" for entry in banter) == 15
    assert sum(entry.mode == "super_italian" for entry in banter) == 6
    assert sum(bool(entry.required_previous_starter_id) for entry in banter) == 3
    assert {entry.required_previous_starter_id for entry in banter if entry.required_previous_starter_id} == {
        "JAMENDO-1215805",
        "USUAN1100173",
    }
    starter_catalog = json.loads(
        (Path(__file__).resolve().parents[2] / "mammamiradio" / "assets" / "starter" / "catalog.json").read_text(
            encoding="utf-8"
        )
    )
    starter_ids = {str(entry["isrc"]) for entry in starter_catalog["tracks"]}
    assert {entry.required_previous_starter_id for entry in banter if entry.required_previous_starter_id} <= starter_ids
    assert sum(entry.special for entry in banter) == 3
    assert is_approved_spoken_asset(recovery[0]) is True
    assert is_approved_spoken_asset(first_listen[0]) is True


def test_missing_manifest_and_unlisted_audio_fail_closed(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    clip = recovery / "mystery.mp3"
    clip.write_bytes(b"x" * 2048)

    assert approved_spoken_assets("recovery", assets_root=tmp_path) == []
    _write_manifest(tmp_path, [])
    errors = validate_spoken_asset_manifest(assets_root=tmp_path)
    assert any("unlisted packaged audio" in error for error in errors)


def test_changed_hash_fails_closed_even_after_path_was_approved(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    clip = recovery / "continuity.mp3"
    original = b"reviewed" * 300
    clip.write_bytes(original)
    _write_manifest(tmp_path, [_entry("recovery/continuity.mp3", original)])
    assert is_approved_spoken_asset(clip, assets_root=tmp_path) is True

    clip.write_bytes(b"changed" * 300)
    assert is_approved_spoken_asset(clip, assets_root=tmp_path) is False
    assert any("sha256 does not match" in error for error in validate_spoken_asset_manifest(assets_root=tmp_path))


def test_listener_arrival_transcript_is_rejected(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    clip = recovery / "unsafe.mp3"
    payload = b"unsafe" * 400
    clip.write_bytes(payload)
    _write_manifest(
        tmp_path,
        [_entry("recovery/unsafe.mp3", payload, transcript="Someone just tuned in.")],
    )

    assert approved_spoken_assets("recovery", assets_root=tmp_path) == []
    assert is_approved_spoken_asset(clip, assets_root=tmp_path) is False
    assert any("listener arrival/return" in error for error in validate_spoken_asset_manifest(assets_root=tmp_path))


def test_runtime_admission_hashes_only_the_selected_asset(tmp_path, monkeypatch):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    selected = recovery / "selected.mp3"
    unrelated = recovery / "unrelated.mp3"
    selected_payload = b"selected" * 400
    unrelated_payload = b"unrelated" * 400
    selected.write_bytes(selected_payload)
    unrelated.write_bytes(unrelated_payload)
    _write_manifest(
        tmp_path,
        [
            _entry("recovery/selected.mp3", selected_payload),
            _entry("recovery/unrelated.mp3", unrelated_payload),
        ],
    )
    hashed_paths: list[Path] = []

    def _record_hash(path: Path) -> str:
        hashed_paths.append(Path(path))
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    monkeypatch.setattr(spoken_assets, "_sha256", _record_hash)

    assert is_approved_spoken_asset(selected, assets_root=tmp_path) is True
    assert hashed_paths == [selected]


def test_banter_metadata_is_mode_safe_and_specials_are_evergreen(tmp_path):
    banter = tmp_path / "banter"
    banter.mkdir()
    clip = banter / "unsafe-mode.mp3"
    payload = b"reviewed" * 400
    clip.write_bytes(payload)
    entry = _entry("banter/unsafe-mode.mp3", payload, language="it")
    entry.update(
        {
            "mode": "normal",
            "required_previous_starter_id": "TRACK-ID",
            "special": True,
        }
    )
    _write_manifest(tmp_path, [entry])

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)

    assert any("language does not match" in error for error in errors)
    assert any("special banter must be evergreen" in error for error in errors)
    assert approved_spoken_asset_entries("banter", assets_root=tmp_path) == []


@pytest.mark.parametrize(
    ("relative_path", "metadata", "expected_error"),
    [
        (
            "banter/clip.mp3",
            {"mode": "festival", "required_previous_starter_id": "", "special": False},
            "banter mode must be",
        ),
        (
            "banter/clip.mp3",
            {"mode": "normal", "required_previous_starter_id": "bad id", "special": False},
            "required starter id is invalid",
        ),
        (
            "recovery/clip.mp3",
            {"mode": "normal", "required_previous_starter_id": "", "special": False},
            "non-banter asset has banter metadata",
        ),
        (
            "banter/clip.mp3",
            {"mode": "normal", "required_previous_starter_id": "", "special": "yes"},
            "banter metadata has invalid types",
        ),
    ],
)
def test_invalid_banter_metadata_fails_closed(tmp_path, relative_path, metadata, expected_error):
    asset_dir = tmp_path / Path(relative_path).parent
    asset_dir.mkdir(parents=True)
    payload = b"reviewed" * 400
    (tmp_path / relative_path).write_bytes(payload)
    entry = _entry(relative_path, payload)
    entry.update(metadata)
    _write_manifest(tmp_path, [entry])

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)

    assert any(expected_error in error for error in errors)


def test_manifested_tone_is_inventory_valid_but_not_spoken(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    tone = recovery / "tone.mp3"
    payload = b"tone" * 600
    tone.write_bytes(payload)
    _write_manifest(
        tmp_path,
        [_entry("recovery/tone.mp3", payload, transcript="", kind="tone", language="none")],
    )

    assert validate_spoken_asset_manifest(assets_root=tmp_path) == []
    assert approved_spoken_assets("recovery", assets_root=tmp_path) == []
    assert is_approved_packaged_audio_asset(tone, assets_root=tmp_path) is True
    assert is_approved_spoken_asset(tone, assets_root=tmp_path) is False

    tone.write_bytes(b"tampered" * 600)
    assert is_approved_packaged_audio_asset(tone, assets_root=tmp_path) is False


def test_local_review_welcome_clip_does_not_invalidate_recovery_manifest(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    clip = recovery / "continuity.mp3"
    payload = b"reviewed" * 300
    clip.write_bytes(payload)
    welcome = tmp_path / "welcome"
    welcome.mkdir()
    (welcome / "local-review.mp3").write_bytes(b"local review only")
    _write_manifest(tmp_path, [_entry("recovery/continuity.mp3", payload)])

    assert validate_spoken_asset_manifest(assets_root=tmp_path) == []
    assert is_approved_spoken_asset(clip, assets_root=tmp_path) is True
    assert approved_spoken_assets("welcome", assets_root=tmp_path) == []


def test_symlink_loop_fails_closed_instead_of_raising(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    loop = recovery / "loop.mp3"
    loop.symlink_to(loop.name)
    _write_manifest(
        tmp_path,
        [
            {
                "path": "recovery/loop.mp3",
                "sha256": "0" * 64,
                "kind": "speech",
                "language": "en",
                "transcript": "The station stays on air.",
            }
        ],
    )

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)

    assert any("escapes the asset root" in error for error in errors)
    assert is_approved_spoken_asset(loop, assets_root=tmp_path) is False


def test_unreadable_manifested_asset_fails_closed_instead_of_raising(tmp_path, monkeypatch):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    clip = recovery / "continuity.mp3"
    payload = b"reviewed" * 300
    clip.write_bytes(payload)
    _write_manifest(tmp_path, [_entry("recovery/continuity.mp3", payload)])
    monkeypatch.setattr(spoken_assets, "_sha256", lambda _path: (_ for _ in ()).throw(OSError("denied")))

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)

    assert any("is unreadable: denied" in error for error in errors)
    assert approved_spoken_assets("recovery", assets_root=tmp_path) == []

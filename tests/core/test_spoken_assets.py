"""Packaged speech must be reviewed, content-addressed, and truth-safe."""

from __future__ import annotations

import hashlib
import json

from mammamiradio.core import spoken_assets
from mammamiradio.core.spoken_assets import (
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


def test_shipped_manifest_is_valid_and_only_continuity_is_spoken():
    assert validate_spoken_asset_manifest() == []
    approved = approved_spoken_assets("recovery")
    assert [path.name for path in approved] == ["continuity_1.mp3"]
    assert is_approved_spoken_asset(approved[0]) is True


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
    assert any("listener arrival/return" in error for error in validate_spoken_asset_manifest(assets_root=tmp_path))


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

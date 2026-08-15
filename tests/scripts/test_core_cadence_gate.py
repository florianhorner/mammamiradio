"""Structural coverage for the approved-treatment core cadence gate."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from scripts import core_cadence_gate as gate


def test_core_inventory_keeps_signature_and_functional_roles_separate() -> None:
    cue_by_id = {spec.id: spec for spec in gate.CORE_CUES}

    assert set(cue_by_id) == {
        "cue.music-to-speech",
        "cue.speech-to-music",
        "cue.ad-in",
        "cue.ad-mid",
        "cue.ad-out",
    }
    assert len({spec.foreground_source_id for spec in cue_by_id.values()}) == len(cue_by_id)
    assert len({spec.path for spec in cue_by_id.values()}) == len(cue_by_id)
    assert all("signature" not in spec.tags for spec in cue_by_id.values())
    assert all(not {"brass", "mandolin", "trumpet"} & set(spec.tags) for spec in cue_by_id.values())

    ad_tags = {
        cue_id: next(tag for tag in cue_by_id[cue_id].tags if tag.startswith("ad-"))
        for cue_id in ("cue.ad-in", "cue.ad-mid", "cue.ad-out")
    }
    assert ad_tags == {
        "cue.ad-in": "ad-bumper",
        "cue.ad-mid": "ad-bumper",
        "cue.ad-out": "ad-bumper",
    }
    assert {"ad-in", "ad-mid", "ad-out"} <= {tag for spec in cue_by_id.values() for tag in spec.tags}


def test_identity_support_carries_neon_rhythm_above_small_speaker_floor() -> None:
    for role in ("station", "sweeper"):
        left, right = gate._identity_support_expressions(role)
        assert "t-0.025" not in left and "t-0.285" not in left
        assert "t-0.025" not in right and "t-0.285" not in right
        assert "392" not in left and "397" not in right
        assert gate.SHA256_RE.fullmatch(gate._identity_support_hash(role))

    transition_specs = [spec for spec in gate.CORE_CUES if "transition" in spec.tags]
    assert all(spec.highpass_hz >= 180 for spec in transition_specs)


def test_runtime_sequences_cover_mid_and_successor_branches_without_repeating_markers() -> None:
    main_label, main_sequence = gate.CADENCE_SEQUENCES["preview.cadence.two-with-mid.music-successor"]
    assert main_label == "Two spots · with mid · music successor"
    assert main_sequence == (
        "render.program-preroll",
        "cue.music-to-speech",
        "render.break.dry.two-with-mid",
        "cue.speech-to-music",
        "context.program-successor",
    )

    with_mid = gate.BREAK_SEQUENCES["render.break.dry.two-with-mid"]
    without_mid = gate.BREAK_SEQUENCES["render.break.dry.two-without-mid"]
    assert with_mid.count("cue.ad-mid") == 1
    assert "cue.ad-mid" not in without_mid
    assert tuple(item for item in with_mid if item != "cue.ad-mid") == without_mid
    for sequence in gate.BREAK_SEQUENCES.values():
        assert sequence.count("cue.ad-in") == 1
        assert sequence.count("cue.ad-out") == 1
        assert sequence.count("source.promo") == 1
    for boundary in ("dry", "adjacent"):
        two_without_mid = gate.BREAK_SEQUENCES[f"render.break.{boundary}.two-without-mid"]
        assert two_without_mid.count("render.spot-a") == 1
        assert two_without_mid.count("render.spot-b") == 1

    with_music = [
        sequence
        for artifact_id, (_label, sequence) in gate.CADENCE_SEQUENCES.items()
        if artifact_id.endswith("music-successor")
    ]
    with_speech = [
        sequence
        for artifact_id, (_label, sequence) in gate.CADENCE_SEQUENCES.items()
        if artifact_id.endswith("speech-successor")
    ]
    assert all("cue.speech-to-music" in sequence for sequence in with_music)
    assert all("cue.speech-to-music" not in sequence for sequence in with_speech)
    assert any("cue.music-to-speech" in sequence for sequence in with_music)
    assert any("cue.music-to-speech" not in sequence for sequence in with_music)


def test_core_receipt_requires_both_listening_targets_and_a_timezone() -> None:
    pack_digest = "a" * 64
    pending = gate._receipt(pack_digest)
    assert gate._validate_receipt(pending, pack_digest=pack_digest) == pending

    approved = {
        **pending,
        "status": "approved",
        "mac": "pass",
        "small_speaker": "pass",
        "sonos_arc": "pass",
        "rationale": "accepted_core_cadence",
        "reviewed_at": datetime.now(UTC).isoformat(),
    }
    assert gate._validate_receipt(approved, pack_digest=pack_digest) == approved

    for field in ("mac", "small_speaker", "sonos_arc"):
        rejected_target = {**approved, field: "fail"}
        with pytest.raises(ValueError, match="pass all three listening targets"):
            gate._validate_receipt(rejected_target, pack_digest=pack_digest)
    timezone_less = {**approved, "reviewed_at": "2026-08-15T22:00:00"}
    with pytest.raises(ValueError, match="timezone"):
        gate._validate_receipt(timezone_less, pack_digest=pack_digest)


def test_core_board_exposes_exactly_five_tracks() -> None:
    groups = gate._preview_groups()

    assert [group["id"] for group in groups] == list(gate.SURFACES)
    assert [group["tracks"] for group in groups] == [
        ["preview.station-id"],
        ["preview.sweeper"],
        ["cue.ad-in"],
        ["cue.ad-out"],
        ["preview.cadence.two-with-mid.music-successor"],
    ]


def test_core_generation_must_not_predate_frozen_speech() -> None:
    gate._require_dependency_chronology("20260815T224501Z", "20260815T224500Z")

    with pytest.raises(ValueError, match="must not predate"):
        gate._require_dependency_chronology("20260815T224459Z", "20260815T224500Z")


def test_non_neon_approved_treatment_is_rejected_before_speech_resolution(tmp_path, monkeypatch) -> None:
    manifest_path = tmp_path / "treatment.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(gate._treatment_gate, "validate_treatment_gate", lambda _path: ({}, ()))
    monkeypatch.setattr(gate._treatment_gate, "assert_treatment_listening_gate", lambda _path: "velvet_horizon")

    with pytest.raises(ValueError, match="requires the approved Neon Relay"):
        gate._resolve_approved_inputs(manifest_path, tmp_path / "speech.json")


def test_core_render_refuses_to_replace_broken_output_symlink(tmp_path, monkeypatch) -> None:
    treatment_manifest = tmp_path / "treatment.json"
    speech_manifest = tmp_path / "speech.json"
    treatment_manifest.write_text("{}\n", encoding="utf-8")
    speech_manifest.write_text("{}\n", encoding="utf-8")
    output_root = tmp_path / "core-board"
    output_root.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    monkeypatch.setattr(gate, "_resolve_approved_inputs", lambda *_args: None)

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        gate.render_core_cadence_gate(treatment_manifest, speech_manifest, output_root)

    assert output_root.is_symlink()


def test_pack_digest_excludes_only_receipt_and_digest_fields() -> None:
    manifest = {
        "schema_version": 1,
        "stage": gate.STAGE,
        "sources": [{"id": "source", "sha256": "b" * 64}],
        "listening_receipt": {"status": "pending"},
        "pack_digest": "c" * 64,
    }
    original = gate._core_pack_digest(manifest)
    decided = deepcopy(manifest)
    decided["listening_receipt"] = {"status": "approved"}
    decided["pack_digest"] = "d" * 64
    assert gate._core_pack_digest(decided) == original

    changed = deepcopy(manifest)
    changed_sources = changed["sources"]
    assert isinstance(changed_sources, list)
    first_source = changed_sources[0]
    assert isinstance(first_source, dict)
    first_source["sha256"] = "e" * 64
    assert gate._core_pack_digest(changed) != original

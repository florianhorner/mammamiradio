from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from scripts import sonic_treatment_gate as gate

CANDIDATE_DURATIONS = {
    "neon_relay": 0.92,
    "velvet_horizon": 1.16,
    "headlight_cut": 0.84,
}
EXPECTED_FORMAT = {
    "codec": "mp3",
    "sample_rate_hz": 48_000,
    "channels": 2,
    "bitrate_kbps": 192,
}
IDENTITY_DIGEST = "a" * 64


def _patch_approved_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    identity_manifest = identity_dir / "manifest.json"
    identity_manifest.write_text("{}\n", encoding="utf-8")
    paths_by_id: dict[str, Path] = {}
    metadata_by_id: dict[str, Mapping[str, object]] = {}
    for clip_id in ("identity-isabella", "identity-marco", "identity-giulia"):
        clip_path = identity_dir / f"{clip_id}.mp3"
        clip_path.write_bytes(f"approved:{clip_id}".encode())
        audio_sha256 = hashlib.sha256(clip_path.read_bytes()).hexdigest()
        paths_by_id[clip_id] = clip_path
        metadata_by_id[clip_id] = {
            "id": clip_id,
            "audio_sha256": audio_sha256,
            "text": f"approved copy for {clip_id}",
        }

    monkeypatch.setattr(
        gate,
        "approved_identity_clips_by_id",
        lambda manifest_path: (
            (
                IDENTITY_DIGEST,
                paths_by_id,
                metadata_by_id,
            )
            if manifest_path == identity_manifest
            else (_ for _ in ()).throw(AssertionError("unexpected identity manifest"))
        ),
    )
    return identity_manifest, str(metadata_by_id["identity-isabella"]["audio_sha256"])


def _patch_audio_tools(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_on_render: int | None = None,
) -> list[Path]:
    rendered_outputs: list[Path] = []

    def write_render(output_path: Path) -> Path:
        rendered_outputs.append(output_path)
        if fail_on_render is not None and len(rendered_outputs) == fail_on_render:
            raise RuntimeError("simulated FFmpeg failure")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"fake-mp3:{output_path.name}".encode())
        return output_path

    def fake_run_ffmpeg(command: Sequence[str], output_path: Path) -> Path:
        assert command
        return write_render(output_path)

    def fake_mix_voice_with_sting(_voice_path: Path, _sting_path: Path, output_path: Path) -> Path:
        return write_render(output_path)

    def fake_probe(path: Path) -> tuple[dict[str, object], float]:
        candidate_id = path.stem.removesuffix("_isabella")
        duration = CANDIDATE_DURATIONS[candidate_id] if path.stem == candidate_id else 4.25
        return dict(EXPECTED_FORMAT), duration

    monkeypatch.setattr(gate, "_run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr(gate, "mix_voice_with_sting", fake_mix_voice_with_sting)
    monkeypatch.setattr(gate, "_probe_audio", fake_probe)
    return rendered_outputs


def _render_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str = "gate") -> tuple[Path, dict[str, Any]]:
    identity_manifest, _isabella_sha256 = _patch_approved_identity(tmp_path, monkeypatch)
    _patch_audio_tools(monkeypatch)
    output_root = tmp_path / name
    manifest = gate.render_treatment_gate(
        identity_manifest,
        output_root,
        generated_at="20260815T203000Z",
    )
    return output_root, manifest


def _write_decision(
    path: Path,
    *,
    pack_digest: str,
    status: str,
    candidate_id: str,
    rationale: str,
    mac: str = "pass",
    sonos_arc: str = "pass",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "pack_digest": pack_digest,
                "status": status,
                "candidate_id": candidate_id,
                "mac": mac,
                "sonos_arc": sonos_arc,
                "rationale": rationale,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_audio_probe_passes_an_absolute_input_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = Path("-treatment.mp3")
    path.write_bytes(b"audio")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_ffprobe(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        payload = {
            "streams": [{"codec_name": "mp3", "sample_rate": "48000", "channels": 2, "bit_rate": "192000"}],
            "format": {"duration": "0.92", "bit_rate": "192000"},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(gate.subprocess, "run", fake_ffprobe)

    audio_format, duration = gate._probe_audio(path)

    assert audio_format == EXPECTED_FORMAT
    assert duration == 0.92
    assert calls == [
        (
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,bit_rate:format=duration,bit_rate",
                "-of",
                "json",
                str(path.resolve()),
            ],
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 10,
            },
        )
    ]


@pytest.mark.parametrize(
    "failure",
    [
        OSError("ffprobe is unavailable"),
        subprocess.TimeoutExpired(["ffprobe"], 10),
    ],
    ids=["os-error", "timeout"],
)
def test_audio_probe_converts_process_failures_to_repairable_value_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    path = tmp_path / "treatment.mp3"

    def fail_ffprobe(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(gate.subprocess, "run", fail_ffprobe)

    with pytest.raises(ValueError) as caught:
        gate._probe_audio(path)

    assert str(caught.value) == (
        "ffprobe could not inspect the treatment output treatment.mp3. "
        "Confirm that ffprobe is installed and responsive, then retry validation or render a fresh board at a new "
        "output path."
    )
    assert caught.value.__cause__ is failure


def test_audio_probe_converts_nonzero_exit_to_repairable_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "treatment.mp3"

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 17, "", "invalid audio data\n"),
    )

    with pytest.raises(ValueError) as caught:
        gate._probe_audio(path)

    assert str(caught.value) == (
        "ffprobe rejected the treatment output treatment.mp3: invalid audio data. "
        "Inspect the source audio, then retry validation or render a fresh board at a new output path."
    )


def test_audio_probe_converts_invalid_json_to_repairable_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "treatment.mp3"

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "not-json", ""),
    )

    with pytest.raises(ValueError) as caught:
        gate._probe_audio(path)

    assert str(caught.value) == (
        "The treatment output treatment.mp3 has unreadable ffprobe evidence. "
        "Inspect the source audio, then retry validation or render a fresh board at a new output path."
    )
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)


def test_render_treatment_gate_binds_approved_identity_and_writes_exact_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_manifest, isabella_sha256 = _patch_approved_identity(tmp_path, monkeypatch)
    rendered_outputs = _patch_audio_tools(monkeypatch)
    output_root = tmp_path / "gate"

    manifest = gate.render_treatment_gate(
        identity_manifest,
        output_root,
        generated_at="20260815T203000Z",
    )
    stored, audio_paths = gate.validate_treatment_gate(output_root / "manifest.json")

    assert stored == manifest
    assert manifest["stage"] == "sonic-treatment-selection"
    assert manifest["generated_at"] == "20260815T203000Z"
    assert manifest["release_ready"] is False
    assert manifest["upstream_identity"]["pack_digest"] == IDENTITY_DIGEST
    assert manifest["upstream_identity"]["isabella_id"] == "identity-isabella"
    assert manifest["upstream_identity"]["isabella_audio_sha256"] == isabella_sha256
    assert manifest["upstream_identity"]["manifest_path_kind"] == "absolute"
    assert Path(manifest["upstream_identity"]["manifest_path"]) == identity_manifest
    assert (output_root / ".ready").read_text(encoding="ascii").strip() == manifest["pack_digest"]
    expected_contract = {
        "voice_id": "identity-isabella",
        "mix_helper": "mammamiradio.audio.normalizer.mix_voice_with_sting",
        "sting_gain_linear": 0.15,
        "voice_gain_linear": 1.2,
        "voice_delay_ms": 400,
    }
    assert dict(gate.PRODUCTION_MIX_CONTRACT) == expected_contract
    assert manifest["production_contract"] == expected_contract
    with pytest.raises(TypeError):
        gate.PRODUCTION_MIX_CONTRACT["voice_id"] = "changed"  # type: ignore[index]
    assert len(rendered_outputs) == 6
    assert len(audio_paths) == 6
    assert set(audio_paths) == {
        output_root / "audio" / f"{candidate_id}{suffix}.mp3"
        for candidate_id in CANDIDATE_DURATIONS
        for suffix in ("", "_isabella")
    }

    candidates = {candidate["id"]: candidate for candidate in manifest["candidates"]}
    assert {candidate_id: candidate["duration_target_sec"] for candidate_id, candidate in candidates.items()} == (
        CANDIDATE_DURATIONS
    )
    output_hashes: set[str] = set()
    for candidate_id, candidate in candidates.items():
        solo = candidate["solo"]
        context = candidate["in_context"]
        assert solo["duration_sec"] == CANDIDATE_DURATIONS[candidate_id]
        assert solo["format"] == EXPECTED_FORMAT
        assert context["format"] == EXPECTED_FORMAT
        assert context["voice_id"] == "identity-isabella"
        assert context["voice_sha256"] == isabella_sha256
        assert context["mix"] == expected_contract
        for asset in (solo, context):
            path = output_root / asset["path"]
            assert asset["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
            output_hashes.add(str(asset["sha256"]))
    assert len(output_hashes) == 6

    assert manifest["listening_receipt"] == {
        "schema_version": 1,
        "status": "pending",
        "pack_digest": manifest["pack_digest"],
        "candidate_id": None,
        "target": "Mac + Wohnzimmer Sonos Arc",
        "prompt": gate.LISTENING_PROMPT,
        "mac": None,
        "sonos_arc": None,
        "rationale": None,
        "reviewed_at": None,
    }
    html = (output_root / "index.html").read_text(encoding="utf-8")
    readme = (output_root / "README.md").read_text(encoding="utf-8")
    assert html.count("<audio ") == 6
    for candidate_id in CANDIDATE_DURATIONS:
        assert f"audio/{candidate_id}.mp3" in html
        assert f"audio/{candidate_id}_isabella.mp3" in html
        assert f"audio/{candidate_id}.mp3" in readme
        assert f"audio/{candidate_id}_isabella.mp3" in readme
    assert "Sonos Arc: pass|fail|not_tested" in html
    assert "Sonos Arc: pass|fail|not_tested" in readme

    complete_surface = "\n".join((json.dumps(manifest, sort_keys=True), html, readme)).casefold()
    for rejected_palette in (
        "midnight signal",
        "midnight_signal",
        "city pulse",
        "city_pulse",
        "warm resolve",
        "warm_resolve",
        "trumpet",
        "mandolin",
        "c-e-g-c",
        "c–e–g–c",
    ):
        assert rejected_palette not in complete_surface


@pytest.mark.parametrize("identity_state", ["pending", "rejected", "tampered"])
def test_render_fails_before_ffmpeg_or_output_when_identity_is_not_currently_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_state: str,
) -> None:
    identity_manifest = tmp_path / f"identity-{identity_state}.json"
    identity_manifest.write_text("{}\n", encoding="utf-8")
    calls = 0

    def fail_identity(_path: Path) -> tuple[str, dict[str, Path], dict[str, Mapping[str, object]]]:
        raise ValueError(f"identity is {identity_state}")

    def forbidden_ffmpeg(_command: Sequence[str], _output_path: Path) -> Path:
        nonlocal calls
        calls += 1
        raise AssertionError("FFmpeg must not run before identity approval")

    monkeypatch.setattr(gate, "approved_identity_clips_by_id", fail_identity)
    monkeypatch.setattr(gate, "_run_ffmpeg", forbidden_ffmpeg)
    output_root = tmp_path / "gate"

    with pytest.raises(ValueError, match=identity_state):
        gate.render_treatment_gate(identity_manifest, output_root, generated_at="20260815T203001Z")

    assert calls == 0
    assert not output_root.exists()


@pytest.mark.parametrize("preexisting_file", [False, True])
def test_render_refuses_output_collisions_without_touching_existing_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preexisting_file: bool,
) -> None:
    identity_manifest, _isabella_sha256 = _patch_approved_identity(tmp_path, monkeypatch)
    output_root = tmp_path / "gate"
    output_root.mkdir()
    sentinel = output_root / "keep.txt"
    if preexisting_file:
        sentinel.write_text("keep", encoding="utf-8")
    rendered_outputs = _patch_audio_tools(monkeypatch)

    with pytest.raises(FileExistsError):
        gate.render_treatment_gate(identity_manifest, output_root, generated_at="20260815T203002Z")

    assert rendered_outputs == []
    assert output_root.is_dir()
    assert not preexisting_file or sentinel.read_text(encoding="utf-8") == "keep"


def test_render_refuses_a_dangling_output_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_manifest, _isabella_sha256 = _patch_approved_identity(tmp_path, monkeypatch)
    rendered_outputs = _patch_audio_tools(monkeypatch)
    output_root = tmp_path / "gate"
    dangling_target = tmp_path / "missing-gate"
    output_root.symlink_to(dangling_target, target_is_directory=True)

    with pytest.raises(FileExistsError, match="Refusing to overwrite treatment gate"):
        gate.render_treatment_gate(identity_manifest, output_root, generated_at="20260815T203002Z")

    assert rendered_outputs == []
    assert output_root.is_symlink()
    assert output_root.resolve(strict=False) == dangling_target


def test_render_refuses_a_dangling_output_symlink_created_during_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_manifest, _isabella_sha256 = _patch_approved_identity(tmp_path, monkeypatch)
    _patch_audio_tools(monkeypatch)
    output_root = tmp_path / "gate"
    dangling_target = tmp_path / "late-missing-gate"
    real_validate = gate.validate_treatment_gate

    def validate_then_create_collision(manifest_path: Path, *, require_ready: bool = True):
        result = real_validate(manifest_path, require_ready=require_ready)
        output_root.symlink_to(dangling_target, target_is_directory=True)
        return result

    monkeypatch.setattr(gate, "validate_treatment_gate", validate_then_create_collision)

    with pytest.raises(FileExistsError, match="Treatment gate appeared during render"):
        gate.render_treatment_gate(identity_manifest, output_root, generated_at="20260815T203002Z")

    assert output_root.is_symlink()
    assert output_root.resolve(strict=False) == dangling_target
    assert not (tmp_path / ".gate.claim").exists()
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".gate.")]


def test_render_failure_removes_claim_and_staging_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_manifest, _isabella_sha256 = _patch_approved_identity(tmp_path, monkeypatch)
    rendered_outputs = _patch_audio_tools(monkeypatch, fail_on_render=3)
    output_root = tmp_path / "gate"

    with pytest.raises(RuntimeError, match="simulated FFmpeg failure"):
        gate.render_treatment_gate(identity_manifest, output_root, generated_at="20260815T203003Z")

    assert len(rendered_outputs) == 3
    assert not output_root.exists()
    assert not [path for path in tmp_path.iterdir() if "gate" in path.name]


def test_render_rejects_output_root_outside_workspace_or_system_tmp(tmp_path: Path) -> None:
    output_root = gate.REPO_ROOT / f".unsafe-treatment-{tmp_path.name}"
    assert not output_root.exists()

    with pytest.raises(ValueError, match="only under"):
        gate.render_treatment_gate(
            tmp_path / "identity.json",
            output_root,
            generated_at="20260815T203004Z",
        )

    assert not output_root.exists()


def test_treatment_receipt_approves_one_candidate_once_and_remains_digest_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, manifest = _render_gate(tmp_path, monkeypatch)
    manifest_path = output_root / "manifest.json"
    approved = _write_decision(
        tmp_path / "approved.json",
        pack_digest=manifest["pack_digest"],
        status="approved",
        candidate_id="velvet_horizon",
        rationale="accepted_sonic_direction",
    )

    gate.write_treatment_listening_receipt_from_decision(
        manifest_path=manifest_path,
        decision_path=approved,
        reviewed_at="2026-08-15T22:00:00+02:00",
    )

    assert gate.assert_treatment_listening_gate(manifest_path) == "velvet_horizon"
    stored, _paths = gate.validate_treatment_gate(manifest_path)
    assert stored["listening_receipt"] == {
        "schema_version": 1,
        "status": "approved",
        "pack_digest": manifest["pack_digest"],
        "candidate_id": "velvet_horizon",
        "target": "Mac + Wohnzimmer Sonos Arc",
        "prompt": gate.LISTENING_PROMPT,
        "mac": "pass",
        "sonos_arc": "pass",
        "rationale": "accepted_sonic_direction",
        "reviewed_at": "2026-08-15T22:00:00+02:00",
    }

    decided_bytes = manifest_path.read_bytes()
    rejected = _write_decision(
        tmp_path / "second.json",
        pack_digest=manifest["pack_digest"],
        status="rejected",
        candidate_id="none",
        rationale="rejected_all_directions",
        mac="fail",
        sonos_arc="fail",
    )
    with pytest.raises(ValueError, match="already been decided"):
        gate.write_treatment_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=rejected,
        )
    assert manifest_path.read_bytes() == decided_bytes


def test_treatment_receipt_records_rejection_of_all_candidates_but_keeps_gate_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, manifest = _render_gate(tmp_path, monkeypatch)
    manifest_path = output_root / "manifest.json"
    rejected = _write_decision(
        tmp_path / "rejected.json",
        pack_digest=manifest["pack_digest"],
        status="rejected",
        candidate_id="none",
        rationale="rejected_all_directions",
        mac="fail",
        sonos_arc="not_tested",
    )

    gate.write_treatment_listening_receipt_from_decision(
        manifest_path=manifest_path,
        decision_path=rejected,
        reviewed_at="2026-08-15T22:05:00+02:00",
    )

    stored, _paths = gate.validate_treatment_gate(manifest_path)
    assert stored["listening_receipt"]["candidate_id"] == "none"
    assert stored["listening_receipt"]["status"] == "rejected"
    assert stored["listening_receipt"]["mac"] == "fail"
    assert stored["listening_receipt"]["sonos_arc"] == "not_tested"
    with pytest.raises(ValueError, match="not approved"):
        gate.assert_treatment_listening_gate(manifest_path)


def test_treatment_receipt_rejects_stale_digest_without_mutating_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, _manifest = _render_gate(tmp_path, monkeypatch)
    manifest_path = output_root / "manifest.json"
    before = manifest_path.read_bytes()
    stale = _write_decision(
        tmp_path / "stale.json",
        pack_digest="f" * 64,
        status="approved",
        candidate_id="neon_relay",
        rationale="accepted_sonic_direction",
    )

    with pytest.raises(ValueError, match="pack_digest"):
        gate.write_treatment_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=stale,
        )

    assert manifest_path.read_bytes() == before
    stored, _paths = gate.validate_treatment_gate(manifest_path)
    assert stored["listening_receipt"]["status"] == "pending"


def test_treatment_validator_binds_exact_surfaces_and_output_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, manifest = _render_gate(tmp_path, monkeypatch)
    manifest_path = output_root / "manifest.json"
    index_path = output_root / "index.html"
    index_path.write_text(index_path.read_text(encoding="utf-8") + "\n<!-- swapped -->\n", encoding="utf-8")
    with pytest.raises(ValueError, match="surface"):
        gate.validate_treatment_gate(manifest_path)

    index_path.write_text(gate._treatment_html(manifest), encoding="utf-8")
    first_audio = output_root / manifest["candidates"][0]["solo"]["path"]
    first_audio.write_bytes(b"tampered-treatment")
    with pytest.raises(ValueError, match="hash"):
        gate.validate_treatment_gate(manifest_path)


def test_approved_receipt_rejects_sonos_failure_without_mutating_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, manifest = _render_gate(tmp_path, monkeypatch)
    manifest_path = output_root / "manifest.json"
    before = manifest_path.read_bytes()
    decision = _write_decision(
        tmp_path / "sonos-failed.json",
        pack_digest=manifest["pack_digest"],
        status="approved",
        candidate_id="headlight_cut",
        rationale="accepted_sonic_direction",
        mac="pass",
        sonos_arc="fail",
    )

    with pytest.raises(ValueError, match="accepted direction"):
        gate.write_treatment_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=decision,
            reviewed_at="2026-08-15T22:10:00+02:00",
        )

    assert manifest_path.read_bytes() == before
    stored, _paths = gate.validate_treatment_gate(manifest_path)
    assert stored["listening_receipt"]["status"] == "pending"


def test_treatment_receipt_rejects_timezone_less_review_time_without_mutating_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, manifest = _render_gate(tmp_path, monkeypatch)
    manifest_path = output_root / "manifest.json"
    before = manifest_path.read_bytes()
    decision = _write_decision(
        tmp_path / "timezone-less.json",
        pack_digest=manifest["pack_digest"],
        status="approved",
        candidate_id="neon_relay",
        rationale="accepted_sonic_direction",
    )

    with pytest.raises(ValueError, match="timezone"):
        gate.write_treatment_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=decision,
            reviewed_at="2026-08-15T22:15:00",
        )

    assert manifest_path.read_bytes() == before
    stored, _paths = gate.validate_treatment_gate(manifest_path)
    assert stored["listening_receipt"]["status"] == "pending"


def test_treatment_validator_rejects_unexpected_board_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root, _manifest = _render_gate(tmp_path, monkeypatch)
    unexpected = output_root / "unexpected.html"
    unexpected.write_text("<script>not part of the listening board</script>\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"inventory.*unexpected: unexpected\.html"):
        gate.validate_treatment_gate(output_root / "manifest.json")


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("simulated post-commit race"),
        subprocess.CalledProcessError(17, ["ffprobe", "manifest-audio.mp3"]),
    ],
    ids=["value-error", "called-process-error"],
)
def test_treatment_receipt_rolls_back_manifest_when_post_commit_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    output_root, manifest = _render_gate(tmp_path, monkeypatch)
    manifest_path = output_root / "manifest.json"
    before = manifest_path.read_bytes()
    decision = _write_decision(
        tmp_path / "post-commit-race.json",
        pack_digest=manifest["pack_digest"],
        status="approved",
        candidate_id="velvet_horizon",
        rationale="accepted_sonic_direction",
    )
    original_validate = gate.validate_treatment_gate
    validation_calls = 0

    def fail_post_commit_validation(
        path: Path,
        *,
        require_ready: bool = True,
    ) -> tuple[dict[str, Any], tuple[Path, ...]]:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 3:
            raise failure
        return original_validate(path, require_ready=require_ready)

    monkeypatch.setattr(gate, "validate_treatment_gate", fail_post_commit_validation)

    with pytest.raises(type(failure)):
        gate.write_treatment_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=decision,
            reviewed_at="2026-08-15T22:20:00+02:00",
        )

    assert validation_calls == 3
    assert manifest_path.read_bytes() == before
    stored, _paths = original_validate(manifest_path)
    assert stored["listening_receipt"]["status"] == "pending"
    assert not list(tmp_path.glob(f".{output_root.name}.*"))

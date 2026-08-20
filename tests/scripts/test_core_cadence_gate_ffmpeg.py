"""Real-FFmpeg contract coverage for the core-cadence listening gate."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from mammamiradio.audio import normalizer
from scripts import core_cadence_gate as gate

pytestmark = pytest.mark.requires_ffmpeg

GENERATED_AT = "20260815T230000Z"
TREATMENT_DIGEST = "a" * 64
IDENTITY_DIGEST = "b" * 64
LOUDNESS_TARGET_LUFS = -16.0
LOUDNESS_TOLERANCE_LU = 2.0
TRUE_PEAK_CEILING_DBFS = -1.0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _generate_audio(path: Path, *, frequency_hz: float, duration_sec: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    left = (
        f"0.13*sin(2*PI*{frequency_hz}*t)"
        f"+0.060*sin(2*PI*{frequency_hz * 2.03}*t)"
        f"+0.025*sin(2*PI*{frequency_hz * 4.01}*t)"
    )
    right = (
        f"0.128*sin(2*PI*{frequency_hz * 1.006}*t)"
        f"+0.058*sin(2*PI*{frequency_hz * 2.045}*t)"
        f"+0.026*sin(2*PI*{frequency_hz * 3.98}*t)"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (f"aevalsrc=exprs='{left}|{right}':s=48000:d={duration_sec}:c=stereo"),
            "-af",
            (
                f"highpass=f=80,lowpass=f=5000,afade=t=in:d=0.03,"
                f"afade=t=out:st={max(duration_sec - 0.20, 0)}:d=0.20,"
                "loudnorm=I=-16:LRA=7:TP=-1.5"
            ),
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-write_xing",
            "0",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _measure_loudness(path: Path) -> tuple[float, float]:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-nostdin",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-16:LRA=11:TP=-1.5:print_format=json",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    reports = re.findall(r'\{\s*"input_i".*?\}', completed.stderr, flags=re.DOTALL)
    assert reports, completed.stderr
    report = json.loads(reports[-1])
    return float(report["input_i"]), float(report["input_tp"])


def _approved_inputs(tmp_path: Path) -> gate.ApprovedInputs:
    treatment_manifest = (tmp_path / "approved-treatment.json").resolve()
    identity_manifest = (tmp_path / "approved-identity.json").resolve()
    speech_manifest = (tmp_path / "approved-speech.json").resolve()
    treatment_manifest.write_text("{}\n", encoding="utf-8")
    identity_manifest.write_text("{}\n", encoding="utf-8")
    speech_manifest.write_text("{}\n", encoding="utf-8")

    signature = (tmp_path / "neon_relay.mp3").resolve()
    isabella = (tmp_path / "identity-isabella.mp3").resolve()
    marco = (tmp_path / "identity-marco.mp3").resolve()
    giulia = (tmp_path / "identity-giulia.mp3").resolve()
    _generate_audio(signature, frequency_hz=98.0, duration_sec=0.92)
    _generate_audio(isabella, frequency_hz=196.0, duration_sec=5.0)
    _generate_audio(marco, frequency_hz=220.0, duration_sec=2.544)
    _generate_audio(giulia, frequency_hz=246.94, duration_sec=3.744)
    paths = {
        "identity-isabella": isabella,
        "identity-marco": marco,
        "identity-giulia": giulia,
    }
    characters = {
        "identity-isabella": "Isabella",
        "identity-marco": "Marco",
        "identity-giulia": "Giulia",
    }
    texts = {
        "identity-isabella": "Mamma Mi Radio. Da Windor a Vergen.",
        "identity-marco": "And now, a word from our sponsors, amici!",
        "identity-giulia": "Back to the music, grazie for staying with us!",
    }
    metadata: dict[str, Mapping[str, object]] = {}
    for voice_id, path in paths.items():
        _audio_format, duration_sec = gate._probe_audio(path)
        metadata[voice_id] = {
            "id": voice_id,
            "character": characters[voice_id],
            "audio_sha256": _sha256(path),
            "duration_sec": duration_sec,
            "text": texts[voice_id],
        }
    speech_specs: dict[str, tuple[float, float, str, str | None, str]] = {
        "speech.sweeper": (293.66, 1.8, "Sei su Mamma Mi Radio.", "identity-isabella", "Isabella"),
        "speech.promo": (329.63, 2.1, "A word from our sponsors, amici.", None, "L'Annunciatore"),
        "speech.spot-a": (349.23, 4.5, "Caffè Aurora, disponibile vicino a te.", "identity-marco", "Marco"),
        "speech.spot-b": (369.99, 5.2, "Luce Notturna, accendila con calma.", "identity-giulia", "Giulia"),
    }
    speech_paths: dict[str, Path] = {}
    speech_metadata: dict[str, Mapping[str, object]] = {}
    for speech_id, (frequency_hz, duration, text, source_voice_id, character) in speech_specs.items():
        path = (tmp_path / f"{speech_id}.mp3").resolve()
        _generate_audio(path, frequency_hz=frequency_hz, duration_sec=duration)
        _audio_format, duration_sec = gate._probe_audio(path)
        provider = "azure" if source_voice_id == "identity-isabella" else "elevenlabs"
        speech_paths[speech_id] = path
        speech_metadata[speech_id] = {
            "id": speech_id,
            "character": character,
            "audio_sha256": _sha256(path),
            "duration_sec": duration_sec,
            "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_kind": "runtime-promo-selection" if source_voice_id is None else "approved-identity",
            "source_voice_id": source_voice_id,
            "provider": provider,
            "voice": "QttbagfgqUCm9K0VgUyT" if source_voice_id is None else f"test-{character.lower()}",
            "model": "azure_speech" if provider == "azure" else "eleven_multilingual_v2",
            "settings": (
                {
                    "stability": 0.42,
                    "similarity_boost": 0.78,
                    "style": 0.45,
                    "use_speaker_boost": True,
                }
                if source_voice_id is None
                else {}
            ),
            "prosody": (
                {
                    "requested_rate": "+40%",
                    "requested_pitch": "-10Hz",
                    "applied": False,
                    "reason": "direct ElevenLabs helper exposes no rate or pitch transform; no transform applied",
                }
                if source_voice_id is None
                else {
                    "requested_rate": None,
                    "requested_pitch": None,
                    "applied": False,
                    "reason": "test fixture",
                }
            ),
        }
    return gate.ApprovedInputs(
        treatment_manifest=treatment_manifest,
        treatment_pack_digest=TREATMENT_DIGEST,
        candidate_id="neon_relay",
        candidate_label="Neon Relay",
        signature_path=signature,
        signature_sha256=_sha256(signature),
        treatment_reviewed_at="2026-08-15T22:55:00+00:00",
        identity_manifest=identity_manifest,
        identity_pack_digest=IDENTITY_DIGEST,
        voice_paths=paths,
        voice_metadata=metadata,
        speech_manifest=speech_manifest,
        speech_pack_digest="c" * 64,
        speech_generated_at="20260815T225959Z",
        speech_paths=speech_paths,
        speech_metadata=speech_metadata,
    )


def _pack_snapshot() -> dict[str, str]:
    root = gate.REPO_ROOT / "mammamiradio" / "assets" / "imaging"
    return {path.relative_to(root).as_posix(): _sha256(path) for path in root.rglob("*") if path.is_file()}


def test_real_ffmpeg_core_gate_is_repeatable_runtime_faithful_and_receipt_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _approved_inputs(tmp_path)

    def approved_inputs(manifest_path: Path, speech_manifest: Path) -> gate.ApprovedInputs:
        assert manifest_path.resolve() == inputs.treatment_manifest
        assert speech_manifest.resolve() == inputs.speech_manifest
        return inputs

    monkeypatch.setattr(gate, "_resolve_approved_inputs", approved_inputs)
    monkeypatch.setattr(normalizer, "_loudness_reconcile", None)
    packaged_before = _pack_snapshot()

    station_hashes: set[str] = set()
    for index in range(5):
        bed_path = gate._render_identity_bed(
            inputs.signature_path,
            tmp_path / f"station-bed-{index}.mp3",
            duration_sec=3.0,
            role="station",
        )
        mixed_path = gate._render_voice_sting_mix(
            inputs.voice_paths["identity-isabella"],
            bed_path,
            tmp_path / f"station-mix-{index}.mp3",
        )
        station_hashes.add(_sha256(mixed_path))
    assert len(station_hashes) == 1

    manifests = [
        gate.render_core_cadence_gate(
            inputs.treatment_manifest,
            inputs.speech_manifest,
            tmp_path / name,
            generated_at=GENERATED_AT,
        )
        for name in ("gate-one", "gate-two")
    ]
    validated = [
        gate.validate_core_cadence_gate(tmp_path / name / "manifest.json") for name in ("gate-one", "gate-two")
    ]

    def assert_semantic_mutation_rejected(
        name: str,
        mutate: Callable[[dict[str, Any]], None],
        match: str,
    ) -> None:
        mutation_root = tmp_path / f"mutation-{name}"
        shutil.copytree(tmp_path / "gate-one", mutation_root)
        changed = deepcopy(manifests[0])
        mutate(changed)
        changed["pack_digest"] = gate._core_pack_digest(changed)
        changed["listening_receipt"] = gate._receipt(changed["pack_digest"])
        (mutation_root / "manifest.json").write_text(
            json.dumps(changed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (mutation_root / "README.md").write_text(gate._readme(changed), encoding="utf-8")
        (mutation_root / "index.html").write_text(gate._html(changed), encoding="utf-8")
        (mutation_root / ".ready").write_text(f"{changed['pack_digest']}\n", encoding="ascii")
        with pytest.raises(ValueError, match=match):
            gate.validate_core_cadence_gate(mutation_root / "manifest.json")

    assert_semantic_mutation_rejected(
        "source-license",
        lambda manifest: manifest["sources"][0].__setitem__("license", "altered"),
        "source inventory",
    )
    assert_semantic_mutation_rejected(
        "layer-gain",
        lambda manifest: next(artifact for artifact in manifest["artifacts"] if artifact["id"] == "cue.ad-in")[
            "layers"
        ][0].__setitem__("gain_db", 6.0),
        "layer provenance",
    )
    assert_semantic_mutation_rejected(
        "break-order",
        lambda manifest: next(
            artifact for artifact in manifest["artifacts"] if artifact["id"] == "render.break.dry.two-with-mid"
        )["layers"].reverse(),
        "layer provenance",
    )
    assert_semantic_mutation_rejected(
        "visible-track",
        lambda manifest: manifest["preview_groups"][-1].__setitem__(
            "tracks", ["preview.cadence.two-without-mid.music-successor"]
        ),
        "listening surface inventory",
    )

    assert manifests[0] == manifests[1]
    assert validated[0][0] == manifests[0]
    assert validated[1][0] == manifests[1]
    assert _pack_snapshot() == packaged_before
    assert manifests[0]["pack_digest"] == manifests[1]["pack_digest"]
    assert manifests[0]["listening_receipt"]["status"] == "pending"
    assert [group["id"] for group in manifests[0]["preview_groups"]] == list(gate.SURFACES)

    artifacts_by_run = [{artifact["id"]: artifact for artifact in manifest["artifacts"]} for manifest in manifests]
    for artifact_id in gate.CADENCE_ARTIFACT_IDS:
        first = artifacts_by_run[0][artifact_id]
        second = artifacts_by_run[1][artifact_id]
        assert first["sha256"] == second["sha256"]
        foregrounds = first["foreground_source_ids"]
        assert len(foregrounds) == len(set(foregrounds))

    main = artifacts_by_run[0]["preview.cadence.two-with-mid.music-successor"]
    assert "generated.transition.music-to-speech" in main["foreground_source_ids"]
    assert "generated.ad-marker.mid" in main["foreground_source_ids"]
    assert "generated.transition.speech-to-music" in main["foreground_source_ids"]
    assert "Alice" not in json.dumps(manifests[0])
    assert {source["id"] for source in manifests[0]["sources"]} >= {
        "identity-isabella",
        "identity-marco",
        "identity-giulia",
        "speech.sweeper",
        "speech.promo",
        "speech.spot-a",
        "speech.spot-b",
    }
    assert sum(len(group["tracks"]) for group in manifests[0]["preview_groups"]) == 5
    mid = artifacts_by_run[0]["cue.ad-mid"]
    assert mid["duration_sec"] == pytest.approx(0.8, abs=0.06)
    assert "fit_audio_oneshot:duration=0.8,target=-16LUFS,fade-out=0.08s" in mid["layers"][0]["dsp"]
    for spot_id, source_id in (("render.spot-a", "speech.spot-a"), ("render.spot-b", "speech.spot-b")):
        spot = artifacts_by_run[0][spot_id]
        assert spot["foreground_source_ids"] == [source_id]
        assert "normalize_ad:compressor,presence,air,highpass,loudnorm" in spot["layers"][0]["dsp"]
    main_break = artifacts_by_run[0]["render.break.dry.two-with-mid"]
    assert [layer["source_id"] for layer in main_break["layers"]] == list(
        gate.BREAK_SEQUENCES["render.break.dry.two-with-mid"]
    )
    translation = manifests[0]["quality_guards"]["small_speaker_translation"]
    assert max(asset["loss_db"] for asset in translation["assets"]) <= 4.0
    assert manifests[0]["quality_guards"]["decoded_audio_correlation"]["max_observed"] < 0.98

    preview_ids = {artifact_id for group in manifests[0]["preview_groups"] for artifact_id in group["tracks"]}
    for artifact_id in preview_ids:
        artifact = artifacts_by_run[0][artifact_id]
        audio_path = tmp_path / "gate-one" / artifact["path"]
        audio_format, duration_sec = gate._probe_audio(audio_path)
        assert audio_format == gate.FORMAT
        assert duration_sec > 0
        integrated_lufs, true_peak_dbfs = _measure_loudness(audio_path)
        assert integrated_lufs == pytest.approx(LOUDNESS_TARGET_LUFS, abs=LOUDNESS_TOLERANCE_LU)
        assert true_peak_dbfs <= TRUE_PEAK_CEILING_DBFS

    manifest_path = tmp_path / "gate-one" / "manifest.json"
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "pack_digest": "0" * 64,
                "status": "approved",
                "mac": "pass",
                "small_speaker": "pass",
                "sonos_arc": "pass",
                "rationale": "accepted_core_cadence",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match"):
        gate.write_core_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=decision_path,
        )
    assert gate.validate_core_cadence_gate(manifest_path)[0]["listening_receipt"]["status"] == "pending"

    decision_path.write_text(
        json.dumps(
            {
                "pack_digest": manifests[0]["pack_digest"],
                "status": "approved",
                "mac": "pass",
                "small_speaker": "pass",
                "sonos_arc": "pass",
                "rationale": "accepted_core_cadence",
            }
        ),
        encoding="utf-8",
    )
    gate.write_core_listening_receipt_from_decision(
        manifest_path=manifest_path,
        decision_path=decision_path,
        reviewed_at="2026-08-15T23:15:00+00:00",
    )
    assert gate.assert_core_listening_gate(manifest_path) == manifests[0]["pack_digest"]

"""Real-FFmpeg contract coverage for the sonic-treatment listening gate."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from mammamiradio.audio import normalizer
from scripts import sonic_treatment_gate as gate

pytestmark = pytest.mark.requires_ffmpeg

GENERATED_AT = "20260815T220000Z"
IDENTITY_DIGEST = "a" * 64
LOUDNESS_TARGET_LUFS = -16.0
LOUDNESS_TOLERANCE_LU = 2.0
# MP3 decoding may overshoot the -1.5 dBTP mastering request slightly.
TRUE_PEAK_CEILING_DBFS = -1.0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _generate_identity_voice(path: Path) -> None:
    """Create a deterministic, voice-band, 48 kHz stereo identity fixture."""
    left = (
        "(0.16*sin(2*PI*(145+4*sin(2*PI*2.1*t))*t)"
        "+0.08*sin(2*PI*(290+5*sin(2*PI*1.7*t))*t)"
        "+0.045*sin(2*PI*725*t)+0.025*sin(2*PI*1160*t))"
        "*(0.72+0.20*sin(2*PI*3.2*t))"
    )
    right = (
        "(0.155*sin(2*PI*(145.8+4.2*sin(2*PI*2.05*t))*t)"
        "+0.078*sin(2*PI*(291.4+4.8*sin(2*PI*1.75*t))*t)"
        "+0.043*sin(2*PI*731*t)+0.024*sin(2*PI*1152*t))"
        "*(0.72+0.20*sin(2*PI*3.15*t))"
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
            f"aevalsrc=exprs='{left}|{right}':s=48000:d=2.8:c=stereo",
            "-af",
            "highpass=f=120,lowpass=f=5000,afade=t=in:d=0.04,afade=t=out:st=2.58:d=0.22,loudnorm=I=-18:LRA=7:TP=-3",
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
    """Return integrated LUFS and measured true peak from loudnorm analysis."""
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
    reports = re.findall(r"\{\s*\"input_i\".*?\}", completed.stderr, flags=re.DOTALL)
    assert reports, completed.stderr
    report = json.loads(reports[-1])
    return float(report["input_i"]), float(report["input_tp"])


def _approved_identity_fixture(
    identity_voice: Path,
) -> tuple[str, dict[str, Path], dict[str, Mapping[str, object]]]:
    voice_hash = _sha256(identity_voice)
    return (
        IDENTITY_DIGEST,
        {"identity-isabella": identity_voice},
        {
            "identity-isabella": {
                "id": "identity-isabella",
                "audio_sha256": voice_hash,
            }
        },
    )


def test_real_ffmpeg_treatment_gate_is_repeatable_and_meets_audio_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_manifest = (tmp_path / "identity-manifest.json").resolve()
    identity_manifest.write_text("{}\n", encoding="utf-8")
    identity_voice = (tmp_path / "identity-isabella.mp3").resolve()
    _generate_identity_voice(identity_voice)
    identity_approval = _approved_identity_fixture(identity_voice)

    def approved_identity_clips_by_id(
        manifest_path: Path,
    ) -> tuple[str, dict[str, Path], dict[str, Mapping[str, object]]]:
        assert manifest_path == identity_manifest
        return identity_approval

    monkeypatch.setattr(gate, "approved_identity_clips_by_id", approved_identity_clips_by_id)
    monkeypatch.setattr(normalizer, "_loudness_reconcile", None)

    identity_format, identity_duration = gate._probe_audio(identity_voice)
    assert identity_format == gate.FORMAT
    assert identity_duration == pytest.approx(2.8, abs=0.08)

    manifests = [
        gate.render_treatment_gate(identity_manifest, tmp_path / name, generated_at=GENERATED_AT)
        for name in ("gate-one", "gate-two")
    ]
    validated = [gate.validate_treatment_gate(tmp_path / name / "manifest.json") for name in ("gate-one", "gate-two")]

    assert manifests[0] == manifests[1]
    assert manifests[0]["pack_digest"] == manifests[1]["pack_digest"]
    assert validated[0][0] == manifests[0]
    assert validated[1][0] == manifests[1]

    candidates_by_run = [{candidate["id"]: candidate for candidate in manifest["candidates"]} for manifest in manifests]
    specs_by_id = {spec.id: spec for spec in gate.TREATMENTS}
    for candidate_id in gate.TREATMENT_IDS:
        first = candidates_by_run[0][candidate_id]
        second = candidates_by_run[1][candidate_id]
        for asset_key in ("solo", "in_context"):
            first_asset = first[asset_key]
            second_asset = second[asset_key]
            first_path = tmp_path / "gate-one" / first_asset["path"]
            second_path = tmp_path / "gate-two" / second_asset["path"]

            assert first_asset["sha256"] == second_asset["sha256"]
            assert first_path.read_bytes() == second_path.read_bytes()

            audio_format, duration_sec = gate._probe_audio(first_path)
            assert audio_format == gate.FORMAT
            assert duration_sec > 0
            if asset_key == "solo":
                assert duration_sec == pytest.approx(specs_by_id[candidate_id].duration_sec, abs=0.05)

            integrated_lufs, true_peak_dbfs = _measure_loudness(first_path)
            assert integrated_lufs == pytest.approx(LOUDNESS_TARGET_LUFS, abs=LOUDNESS_TOLERANCE_LU)
            assert true_peak_dbfs <= TRUE_PEAK_CEILING_DBFS

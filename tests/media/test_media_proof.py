from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "media-proof.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("media_proof_tool", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_quick_gate_is_strict_and_actionable_while_auditions_are_pending(tool) -> None:
    report = tool.run_quick()

    assert report["exit_code"] == 1
    assert report["ok"] is False
    assert tuple(report["groups"]) == tool.GROUPS_QUICK
    assert report["groups"]["MEDIA-EVIDENCE"]["status"] == "FAIL"
    assert report["groups"]["MEDIA-BYTES"]["status"] == "FAIL"
    assert report["groups"]["MEDIA-AUDIO"]["status"] == "FAIL"
    assert report["groups"]["MEDIA-PACKAGE"]["status"] == "PASS"
    evidence = report["groups"]["MEDIA-EVIDENCE"]["checks"][0]
    assert evidence["manifest"] == {
        "catalog_id": "starter-v1",
        "path": "mammamiradio/assets/starter/catalog.json",
    }
    assert (evidence["actual"], evidence["expected"]) == (0, 12)
    assert evidence["next_command"].startswith("python scripts/starter-catalog.py acquire --isrc ")


def test_quick_json_mode_has_same_stable_diagnostics_and_exit_code(tmp_path: Path) -> None:
    output = tmp_path / "media-proof.json"
    result = subprocess.run(
        [sys.executable, os.fspath(SCRIPT), "--quick", "--json", "--output", os.fspath(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    report = json.loads(result.stdout)
    assert result.returncode == report["exit_code"] == 1
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert tuple(sorted(report["groups"])) == tuple(
        sorted(("MEDIA-EVIDENCE", "MEDIA-BYTES", "MEDIA-AUDIO", "MEDIA-PACKAGE"))
    )
    assert "client_id" not in result.stdout.casefold()


def test_proof_paths_reject_context_and_symlink_outputs(tmp_path: Path, tool) -> None:
    with pytest.raises(tool.ProofToolError, match=r"under \.context"):
        tool._safe_output_path(".context/media-proof.json", label="proof output")

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "proof.json"
    link.symlink_to(target)
    with pytest.raises(tool.ProofToolError, match="symlink"):
        tool._safe_output_path(os.fspath(link), label="proof output")

    with pytest.raises(tool.ProofToolError, match="ignored tmp/media-proof"):
        tool._safe_work_base("build/media-proof")
    assert tool._safe_work_base("tmp/media-proof/retained") == ROOT / "tmp" / "media-proof" / "retained"


def test_report_writer_is_atomic_and_machine_readable(tmp_path: Path, tool) -> None:
    path = tmp_path / "nested" / "media-proof.json"
    report = {"schema_version": "1", "ok": False, "exit_code": 1}

    tool._write_report(path, report)

    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert not list(path.parent.glob(f".{path.name}.*"))


def test_archive_readers_normalize_wheel_and_sdist_starter_paths(tmp_path: Path, tool) -> None:
    expected = {
        "mammamiradio/assets/starter/catalog.json": b"{}",
        "mammamiradio/assets/starter/tracks/example.mp3": b"mp3",
    }
    wheel = tmp_path / "example.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in expected.items():
            archive.writestr(name, payload)
        archive.writestr("mammamiradio/not-starter.txt", b"ignored")

    sdist = tmp_path / "example.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name, payload in expected.items():
            info = tarfile.TarInfo(f"mammamiradio-1.0/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    wheel_files = tool._wheel_starter_files(wheel)
    sdist_files = tool._sdist_starter_files(sdist)
    assert wheel_files == sdist_files == expected
    assert tool._parity_summary(expected, wheel_files) == (True, "2 starter package file(s) match byte-for-byte")


def test_archive_parity_reports_missing_extra_and_changed_bytes(tool) -> None:
    ok, summary = tool._parity_summary(
        {"mammamiradio/assets/starter/catalog.json": b"expected", "missing.mp3": b"x"},
        {"mammamiradio/assets/starter/catalog.json": b"changed", "extra.mp3": b"y"},
    )

    assert ok is False
    assert "missing=missing.mp3" in summary
    assert "extra=extra.mp3" in summary
    assert "changed=mammamiradio/assets/starter/catalog.json" in summary


def _approved_archive_catalog(*, durations: tuple[float, ...]) -> dict[str, object]:
    return {
        "catalog_id": "starter-v1",
        "release_requirements": {
            "exact_track_count": len(durations),
            "minimum_duration_seconds": 2700,
        },
        "tracks": [
            {
                "isrc": f"USUAN990000{index}",
                "approval": {"status": "approved"},
                "derivative": {
                    "path": f"tracks/track-{index}.mp3",
                    "codec": "mp3",
                    "sample_rate_hz": 48000,
                    "channels": 2,
                    "bitrate_kbps": 192,
                    "duration_seconds": duration,
                    "integrated_loudness_lufs": -16.0,
                },
            }
            for index, duration in enumerate(durations)
        ],
    }


def test_archive_audio_gate_uses_ebur128_and_measured_catalog_duration(tmp_path: Path, monkeypatch, tool) -> None:
    catalog = _approved_archive_catalog(durations=(1350.0, 1349.0))
    files = {f"mammamiradio/assets/starter/tracks/track-{index}.mp3": b"audio" for index in range(2)}
    measured_durations = iter((1350.0, 1349.0))
    measured_loudness = iter((-16.0, -14.9))

    def fake_probe(_payload: bytes, _destination: Path) -> dict[str, object]:
        return {
            "codec": "mp3",
            "sample_rate_hz": 48000,
            "channels": 2,
            "bitrate_kbps": 192,
            "duration_seconds": next(measured_durations),
        }

    monkeypatch.setattr(tool, "_probe_bytes", fake_probe)
    monkeypatch.setattr(tool, "_measure_packaged_lufs", lambda _path: next(measured_loudness))
    report = tool._new_report(mode="full", catalog=catalog)

    tool._probe_archive_derivatives(report, catalog, "wheel", files, tmp_path)

    check = report["groups"]["MEDIA-AUDIO"]["checks"][-1]
    assert check["status"] == "FAIL"
    assert "USUAN9900001:loudness=-14.9" in check["message"]
    assert "catalog_duration=2699.000s<2700s" in check["message"]
    assert check["actual"]["measured_duration_seconds"] == 2699.0


def test_archive_audio_gate_still_remeasures_approved_rows_when_catalog_is_incomplete(
    tmp_path: Path, monkeypatch, tool
) -> None:
    catalog = _approved_archive_catalog(durations=(2700.0,))
    requirements = catalog["release_requirements"]
    assert isinstance(requirements, dict)
    requirements["exact_track_count"] = 2
    files = {"mammamiradio/assets/starter/tracks/track-0.mp3": b"audio"}
    probed: list[Path] = []

    def fake_probe(_payload: bytes, destination: Path) -> dict[str, object]:
        probed.append(destination)
        return {
            "codec": "mp3",
            "sample_rate_hz": 48000,
            "channels": 2,
            "bitrate_kbps": 192,
            "duration_seconds": 2700.0,
        }

    monkeypatch.setattr(tool, "_probe_bytes", fake_probe)
    monkeypatch.setattr(tool, "_measure_packaged_lufs", lambda _path: -16.0)
    report = tool._new_report(mode="full", catalog=catalog)

    tool._probe_archive_derivatives(report, catalog, "sdist", files, tmp_path)

    checks = report["groups"]["MEDIA-AUDIO"]["checks"]
    assert [check["code"] for check in checks] == ["sdist_derivative_count", "sdist_ffprobe"]
    assert len(probed) == 1
    assert checks[-1]["actual"] == {"validated_tracks": 1, "measured_duration_seconds": 2700.0}


def test_image_audio_gate_requires_measured_duration_and_every_approved_track(tool) -> None:
    catalog = _approved_archive_catalog(durations=(1350.0, 1350.0))
    complete = {
        "audio_failures": [],
        "audio_checked": 2,
        "audio_duration_seconds": 2700.0,
        "audio_loudness_lufs": {"USUAN9900000": -16.0, "USUAN9900001": -15.0},
    }

    assert tool._image_audio_is_complete(complete, catalog) is True
    assert tool._image_audio_is_complete({**complete, "audio_duration_seconds": 2699.999}, catalog) is False
    assert tool._image_audio_is_complete({**complete, "audio_checked": 1}, catalog) is False
    assert tool._image_audio_is_complete({**complete, "audio_failures": ["track:loudness"]}, catalog) is False
    assert (
        tool._image_audio_is_complete(
            {**complete, "audio_loudness_lufs": {"USUAN9900000": -16.0, "USUAN9900001": -14.9}}, catalog
        )
        is False
    )


def test_image_probe_contract_runs_ebur128_and_reports_actual_duration(tool) -> None:
    compile(tool.IMAGE_PROBE, "<image-proof>", "exec")
    assert "ebur128=peak=true" in tool.IMAGE_PROBE
    assert 'result["audio_duration_seconds"] += duration_seconds' in tool.IMAGE_PROBE
    assert 'result["audio_loudness_lufs"][isrc] = measured_lufs' in tool.IMAGE_PROBE
    assert "STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS" in tool.IMAGE_PROBE
    assert "STARTER_MINIMUM_DURATION_SECONDS" in tool.IMAGE_PROBE


def test_current_extractor_and_transience_static_boundaries_are_closed(tool) -> None:
    metadata_ok, _ = tool._pyproject_extractor_boundary(ROOT / "pyproject.toml")
    addon_ok, _ = tool._shared_addon_boundary()
    transient_ok, _ = tool._transient_static_boundary()

    assert metadata_ok is True
    assert tool._direct_extractor_imports() == []
    assert addon_ok is True
    assert transient_ok is True


def test_lazy_import_scan_rejects_eager_or_non_gateway_imports(tmp_path: Path, tool) -> None:
    gateway = tmp_path / "mammamiradio" / "playlist" / "downloader.py"
    gateway.parent.mkdir(parents=True)
    gateway.write_text("def load():\n    import yt_dlp\n", encoding="utf-8")
    bad = tmp_path / "mammamiradio" / "other.py"
    bad.write_text("import yt_dlp\n", encoding="utf-8")

    failures = tool._direct_extractor_imports(tmp_path)

    assert failures == ["mammamiradio/other.py:1"]


def test_wheel_metadata_requires_exactly_one_optional_ytdlp_requirement(tool) -> None:
    assert tool._yt_dlp_metadata_is_optional(["fastapi>=0.115", 'yt-dlp>=2026.6.9; extra == "external-media"'])
    assert not tool._yt_dlp_metadata_is_optional(["yt-dlp>=2026.6.9"])
    assert not tool._yt_dlp_metadata_is_optional(
        [
            'yt-dlp>=2026.6.9; extra == "external-media"',
            'yt-dlp>=2027; extra == "external-media"',
        ]
    )


def test_requirements_scan_rejects_normalized_ytdlp_names(tmp_path: Path, tool) -> None:
    clean = tmp_path / "requirements-clean.txt"
    clean.write_text("fastapi==0.116.0 \\\n    --hash=sha256:abc\n", encoding="utf-8")
    dirty = tmp_path / "requirements-dirty.txt"
    dirty.write_text(
        "# generated\nYT_DLP==2026.6.9\n-e git+https://example.invalid/tool#egg=yt-dlp\n",
        encoding="utf-8",
    )

    assert tool._requirements_extractor_entries(clean) == []
    assert tool._requirements_extractor_entries(dirty) == [
        "requirements-dirty.txt:2:YT_DLP==2026.6.9",
        "requirements-dirty.txt:3:-e git+https://example.invalid/tool#egg=yt-dlp",
    ]


def test_archive_extractor_scan_detects_vendored_module(tmp_path: Path, tool) -> None:
    clean = tmp_path / "clean.whl"
    with zipfile.ZipFile(clean, "w") as archive:
        archive.writestr("mammamiradio/__init__.py", b"")
    dirty = tmp_path / "dirty.whl"
    with zipfile.ZipFile(dirty, "w") as archive:
        archive.writestr("yt_dlp/__init__.py", b"")

    assert tool._archive_extractor_files(clean) == []
    assert tool._archive_extractor_files(dirty) == ["yt_dlp/__init__.py"]


def test_image_probe_inspects_local_image_before_network_disabled_run(monkeypatch, tool) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="linux/amd64\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"files": {}, "audio_failures": [], "extractor_failures": []}),
            stderr="",
        )

    monkeypatch.setattr(tool.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    result = tool._inspect_image("local/mammamiradio:proof", "amd64")

    assert result["extractor_failures"] == []
    assert calls[0][:4] == ["/usr/bin/docker", "image", "inspect", "--format"]
    assert calls[1][:8] == [
        "/usr/bin/docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--platform",
        "linux/amd64",
        "-i",
    ]
    assert all("pull" not in command for command in calls)


def test_missing_local_image_is_tooling_error_and_never_pulled(monkeypatch, tool) -> None:
    monkeypatch.setattr(tool.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(
        tool.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="not found"),
    )

    with pytest.raises(tool.ProofToolError, match="never pulls images"):
        tool._inspect_image("missing:proof", "aarch64")


def test_makefile_exposes_strict_check_and_json_proof_targets() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "media-check:" in makefile
    assert "scripts/media-proof.py --quick" in makefile
    assert "media-proof:" in makefile
    assert "tmp/media-proof/media-proof.json" in makefile
    assert ".context" not in "\n".join(line for line in makefile.splitlines() if "media-proof.py" in line)

"""Focused contract tests for the complete audio-pack listening gate."""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import complete_audio_pack_gate as gate


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _fake_recipe_bytes(
    voice_path: Path,
    bed_path: Path,
    bed_gain_db: float,
    cues: tuple[tuple[Path, str, float, float], tuple[Path, str, float, float]],
) -> bytes:
    parts = [voice_path.read_bytes(), bed_path.read_bytes(), repr(bed_gain_db).encode()]
    for cue_path, anchor, gain_db, max_duration_sec in cues:
        parts.extend(
            [
                cue_path.read_bytes(),
                anchor.encode(),
                repr(gain_db).encode(),
                repr(max_duration_sec).encode(),
            ]
        )
    return b"\0".join(parts)


def _fake_render_recipe_preview(
    voice_path: Path,
    bed_path: Path,
    bed_gain_db: float,
    cues: tuple[tuple[Path, str, float, float], tuple[Path, str, float, float]],
    output_path: Path,
) -> Path:
    return _write(output_path, _fake_recipe_bytes(voice_path, bed_path, bed_gain_db, cues))


def _fake_render_casa_preview(voice_path: Path, bed_path: Path, output_path: Path) -> Path:
    return _write(output_path, b"casa\0" + voice_path.read_bytes() + b"\0" + bed_path.read_bytes())


def _generator_evidence(
    revision: str = "a" * 40,
    *,
    dependency_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    hashes = dependency_hashes or {
        path: hashlib.sha256(f"fixture dependency::{path}".encode()).hexdigest() for path in gate.GENERATOR_DEPENDENCIES
    }
    dependencies = [
        {
            "path": path,
            "url": f"https://github.com/florianhorner/mammamiradio/blob/{revision}/{path}",
            "sha256": hashes[path],
        }
        for path in gate.GENERATOR_DEPENDENCIES
    ]
    primary = next(
        dependency for dependency in dependencies if dependency["path"] == "scripts/complete_audio_pack_gate.py"
    )
    return {
        "kind": "git-committed-ffmpeg-generator",
        "revision": revision,
        "path": primary["path"],
        "url": primary["url"],
        "sha256": primary["sha256"],
        "dependencies": dependencies,
        "runtime": {
            "python": "3.12.fixture",
            "ffmpeg": "ffmpeg fixture",
        },
    }


def _runtime_audio_toml() -> bytes:
    lines = [
        "[audio]",
        "sample_rate = 48000",
        "channels = 2",
        "bitrate = 192",
        "lufs_target = -16.0",
        "ad_lufs_target = -15.0",
        "broadcast_chain = false",
        "",
        "[imaging]",
        "bed_volume_db = -18.0",
        'assets_dir = ""',
    ]
    for name, (category, recipe_id) in gate.EXPECTED_OFFICIAL_BRAND_CONTRACT.items():
        lines.extend(
            (
                "",
                "[[ads.brands]]",
                f'name = "{name}"',
                f'category = "{category}"',
                f'sonic_recipe = "{recipe_id}"',
            )
        )
    return ("\n".join(lines) + "\n").encode()


@dataclass
class Candidate:
    run_root: Path
    pack_dir: Path
    manifest_path: Path
    core_manifest_path: Path
    manifest: dict[str, Any]
    generator: dict[str, Any]
    speech_paths: dict[str, Path]
    core_paths: dict[str, Path]
    decisions_dir: Path
    runtime_imaging_calls: list[tuple[object, ...]]

    @property
    def ready_path(self) -> Path:
        return self.run_root / ".ready"

    @property
    def receipt_lock_path(self) -> Path:
        return self.run_root.parent / f".{self.run_root.name}.complete-listening-receipt.lock"

    def asset(self, asset_id: str) -> dict[str, Any]:
        return next(item for item in self.manifest["assets"] if item["id"] == asset_id)

    def preview(self, preview_id: str) -> dict[str, Any]:
        return next(item for item in self.manifest["previews"] if item["id"] == preview_id)

    def rewrite_asset(self, asset_id: str, content: bytes) -> None:
        asset = self.asset(asset_id)
        delivered = self.pack_dir / asset["path"]
        delivered.write_bytes(content)
        asset["sha256"] = gate._sha256(delivered)
        source = next(item for item in self.manifest["sources"] if item["id"] == asset["source_ids"][0])
        master = (self.pack_dir / source["original_path"]).resolve()
        master.write_bytes(content)
        source["source_sha256"] = gate._sha256(master)
        asset["layers"][0]["source_sha256"] = source["source_sha256"]

    def save(
        self,
        *,
        refresh_board: bool = True,
        refresh_ready: bool = True,
    ) -> None:
        if refresh_board:
            gate._render_board(self.run_root, self.manifest)
            gate._write_handoff(self.run_root)
            self.manifest["board_files"] = gate._board_file_records(self.run_root)
        self.manifest["pack_digest"] = gate._generic_pack_digest(self.manifest["assets"])
        self.manifest["content_digest"] = gate._json_digest(gate._content_digest_payload(self.manifest))
        receipt = self.manifest.get("listening_receipt")
        if isinstance(receipt, dict):
            receipt["pack_digest"] = self.manifest["pack_digest"]
            receipt["content_digest"] = self.manifest["content_digest"]
        else:
            self.manifest["listening_receipt"] = gate._receipt(
                self.manifest["pack_digest"], self.manifest["content_digest"]
            )
        gate._write_json(self.manifest_path, self.manifest)
        if refresh_ready:
            self.ready_path.write_text(str(self.manifest["content_digest"]) + "\n", encoding="utf-8")

    def approve(self, reviewed_at: str | None = None) -> None:
        self.manifest["release_ready"] = True
        self.manifest["listening_receipt"] = {
            "schema_version": gate.RECEIPT_SCHEMA_VERSION,
            "status": "approved",
            "pack_digest": self.manifest["pack_digest"],
            "content_digest": self.manifest["content_digest"],
            "approved_candidate_id": "modern-night-drive-complete",
            "target": gate.TARGET,
            "surfaces": list(gate.REQUIRED_SURFACES),
            "device_classes": list(gate.REQUIRED_DEVICE_CLASSES),
            "prompt": gate.LISTENING_PROMPT,
            "mac": "pass",
            "small_speaker": "pass",
            "sonos_arc": "pass",
            "notes": "Approved after all target-device checks.",
            "reviewed_at": reviewed_at or datetime.now(UTC).isoformat(),
        }
        self.save()

    def write_decision(
        self,
        *,
        status: str = "approved",
        mac: str = "pass",
        small_speaker: str = "pass",
        sonos_arc: str = "pass",
        notes: str = "Listening decision from the focused fixture.",
    ) -> Path:
        decision_path = self.decisions_dir / f"decision-{status}.json"
        gate._write_json(
            decision_path,
            {
                "content_digest": self.manifest["content_digest"],
                "status": status,
                "mac": mac,
                "small_speaker": small_speaker,
                "sonos_arc": sonos_arc,
                "notes": notes,
            },
        )
        return decision_path


@pytest.fixture
def candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Candidate:
    run_root = tmp_path / "candidate"
    pack_dir = run_root / "pack"
    pack_dir.mkdir(parents=True)

    generator = _generator_evidence()
    monkeypatch.setattr(gate, "_pinned_generator_evidence", lambda: copy.deepcopy(generator))

    core_root = tmp_path / "approved-core"
    core_manifest_path = core_root / "manifest.json"
    artifact_ids = {
        *(artifact_id for artifact_id, *_rest in gate.CORE_PACK_ASSETS),
        "cue.ad-mid",
        *(artifact_id for artifact_id, _label in gate.CORE_PREVIEW_ARTIFACTS),
    }
    core_records: list[dict[str, Any]] = []
    core_paths: dict[str, Path] = {}
    lineage_artifact_ids = {
        *(artifact_id for artifact_id, *_rest in gate.CORE_PACK_ASSETS),
        "cue.ad-mid",
    }
    core_sources: list[dict[str, Any]] = []
    core_source_by_id: dict[str, dict[str, Any]] = {}

    def core_source(source_id: str, license_id: str) -> dict[str, Any]:
        existing = core_source_by_id.get(source_id)
        if existing is not None:
            return existing
        source = {
            "id": source_id,
            "kind": "fixture-approved-core-source",
            "license": license_id,
            "source_sha256": hashlib.sha256(f"approved-source::{source_id}".encode()).hexdigest(),
            "title": f"Approved source {source_id}",
            "upstream_metadata": {"fixture": True, "source_id": source_id},
        }
        core_source_by_id[source_id] = source
        core_sources.append(source)
        return source

    signature_source = core_source("approved-treatment.signature", "audition-only-approved-direction")
    station_support = core_source("generated.identity-support.station", "audition-only-local-generation")
    sweeper_support = core_source("generated.identity-support.sweeper", "audition-only-local-generation")
    cue_source_ids = {
        "cue.music-to-speech": "generated.transition.music-to-speech",
        "cue.speech-to-music": "generated.transition.speech-to-music",
        "cue.ad-in": "generated.ad-marker.in",
        "cue.ad-mid": "generated.ad-marker.mid",
        "cue.ad-out": "generated.ad-marker.out",
    }
    for source_id in cue_source_ids.values():
        core_source(source_id, "audition-only-local-generation")

    def layer(
        source: dict[str, Any],
        *,
        role: str,
        gain_db: float,
        dsp: list[str],
        source_start_sec: float = 0.0,
        duration_sec: float = 1.0,
        output_offset_sec: float = 0.0,
    ) -> dict[str, Any]:
        return {
            "source_id": source["id"],
            "source_sha256": source["source_sha256"],
            "source_start_sec": source_start_sec,
            "duration_sec": duration_sec,
            "output_offset_sec": output_offset_sec,
            "gain_db": gain_db,
            "dsp": dsp,
            "license": source["license"],
            "role": role,
        }

    for artifact_id in sorted(artifact_ids):
        relative = f"artifacts/{artifact_id.replace('.', '_')}.mp3"
        path = _write(core_root / relative, f"approved-core::{artifact_id}".encode())
        core_paths[artifact_id] = path
        record: dict[str, Any] = {"id": artifact_id, "path": relative, "sha256": gate._sha256(path)}
        if artifact_id in {"render.station-bed", "render.sweeper-bed"}:
            support = station_support if artifact_id == "render.station-bed" else sweeper_support
            record["layers"] = [
                layer(
                    signature_source,
                    role="foreground",
                    gain_db=-15.0,
                    dsp=["copy:exact-approved-artifact", "identity-bed-gain:-15dB"],
                    duration_sec=0.96,
                ),
                layer(
                    signature_source,
                    role="texture",
                    gain_db=9.0,
                    dsp=["identity-derived-midrange", "asoftclip:tanh"],
                    duration_sec=0.96,
                ),
                layer(
                    support,
                    role="texture",
                    gain_db=4.0,
                    dsp=["highpass:150Hz", "lowpass:5200Hz", "loudnorm:I=-16"],
                ),
            ]
            record["foreground_source_ids"] = [signature_source["id"]]
        elif artifact_id in lineage_artifact_ids:
            source = core_source_by_id[cue_source_ids[artifact_id]]
            cue_layer = layer(
                source,
                role="foreground",
                gain_db=-1.25,
                dsp=["generator:ffmpeg-aevalsrc", "highpass:180Hz", "loudnorm:I=-16"],
                source_start_sec=0.037,
                duration_sec=0.68,
                output_offset_sec=0.021,
            )
            if artifact_id == "cue.ad-mid":
                cue_layer["rendered_master_sha256"] = gate._sha256(path)
            record["layers"] = [cue_layer]
            record["foreground_source_ids"] = [source["id"]]
        core_records.append(record)
    core_manifest = {"sources": core_sources, "artifacts": core_records}
    gate._write_json(core_manifest_path, core_manifest)
    core_digest = "b" * 64
    monkeypatch.setattr(gate._core, "_path_record", lambda path: (str(Path(path).resolve()), "absolute"))
    monkeypatch.setattr(
        gate._core,
        "_path_from_record",
        lambda path, _kind, *, label: Path(str(path)).resolve(strict=True),
    )
    monkeypatch.setattr(gate._core, "assert_core_listening_gate", lambda _path: core_digest)
    monkeypatch.setattr(gate._core, "validate_core_cadence_gate", lambda _path: (core_manifest, {}))
    monkeypatch.setattr(gate._core, "_probe_audio", lambda _path: (dict(gate.FORMAT), 1.0))

    def fake_audio_quality(
        _path: Path,
        *,
        quality_class: str,
        target_lufs: float,
        tolerance_lu: float,
    ) -> dict[str, object]:
        return {
            "class": quality_class,
            "measurement": gate.AUDIO_QUALITY_CONTRACT["measurement"],
            "integrated_lufs": target_lufs,
            "target_lufs": target_lufs,
            "tolerance_lu": tolerance_lu,
            "true_peak_dbtp": -2.0,
            "true_peak_ceiling_dbtp": gate.AUDIO_QUALITY_CONTRACT["true_peak_ceiling_dbtp"],
            "exception": None,
        }

    monkeypatch.setattr(gate, "_audio_quality_evidence", fake_audio_quality)

    monkeypatch.setattr(
        gate._validator,
        "validate_audio_asset_pack",
        lambda *_args, **_kwargs: SimpleNamespace(pack_dir=pack_dir),
    )
    monkeypatch.setattr(gate._validator, "check_attribution", lambda _report: None)
    monkeypatch.setattr(
        gate,
        "_render_runtime_mid_from_pack",
        lambda runtime_pack, output: shutil.copy2(runtime_pack / "bumpers" / "ad_mid.mp3", output),
    )
    monkeypatch.setattr(gate, "_render_recipe_preview", _fake_render_recipe_preview)
    monkeypatch.setattr(gate, "_render_casa_notte_preview", _fake_render_casa_preview)

    runtime_imaging_calls: list[tuple[object, ...]] = []

    class FakeRuntimeImaging:
        def __init__(self, runtime_pack: Path) -> None:
            self.runtime_pack = runtime_pack

        def pick_time_check_sting(self, destination: Path) -> Path:
            runtime_imaging_calls.append(("pick_time_check_sting", destination))
            return Path(shutil.copy2(self.runtime_pack / gate.TIME_CHECK.path, destination))

        def resolve_ad_recipe(self, recipe_id: str, variant_key: str) -> SimpleNamespace:
            runtime_imaging_calls.append(("resolve_ad_recipe", recipe_id, variant_key))
            definition = next(item for item in gate.RECIPE_DEFINITIONS if item.recipe_id == recipe_id)
            bed = next(
                item
                for item in gate.RECIPE_BEDS
                if item.asset_id.removeprefix("ad-bed.").replace("-", "_") == recipe_id
            )
            return SimpleNamespace(
                bed_path=self.runtime_pack / bed.path,
                bed_gain_db=definition.bed_gain_db,
                cues=(
                    SimpleNamespace(
                        asset_path=self.runtime_pack / definition.cue_one.path,
                        anchor="intro",
                        gain_db=-13.0,
                        max_duration_sec=definition.cue_one.duration_sec,
                    ),
                    SimpleNamespace(
                        asset_path=self.runtime_pack / definition.cue_two.path,
                        anchor="outro",
                        gain_db=-12.5,
                        max_duration_sec=definition.cue_two.duration_sec,
                    ),
                ),
            )

    monkeypatch.setattr(
        gate,
        "_runtime_imaging_library",
        lambda runtime_pack, _scratch: FakeRuntimeImaging(runtime_pack),
    )

    speech_paths = {
        "speech.spot-a": _write(tmp_path / "speech" / "spot-a.mp3", b"frozen-spot-a"),
        "speech.spot-b": _write(tmp_path / "speech" / "spot-b.mp3", b"frozen-spot-b"),
    }
    monkeypatch.setattr(
        gate,
        "_resolve_frozen_spot_speech",
        lambda _manifest: (speech_paths, {key: {"id": key} for key in speech_paths}),
    )

    sources: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    core_asset_artifact = {asset_id: artifact_id for artifact_id, asset_id, *_rest in gate.CORE_PACK_ASSETS}
    expected_metadata = gate._expected_asset_metadata_contract()
    expected_source_metadata = gate._expected_source_metadata_contract(generator)
    for asset_id, (relative, kind) in gate._expected_asset_contract().items():
        if asset_id in core_asset_artifact:
            content = core_paths[core_asset_artifact[asset_id]].read_bytes()
        elif asset_id == "bumper.ad-mid":
            content = core_paths["cue.ad-mid"].read_bytes()
        else:
            content = f"generated-master::{asset_id}".encode()
        delivered = _write(pack_dir / relative, content)
        master = _write(pack_dir / "provenance" / "source-masters" / f"{asset_id}.mp3", content)
        metadata = expected_metadata[asset_id]
        source_id = str(metadata["source_id"])
        raw_tags = metadata["tags"]
        raw_dsp = metadata["dsp"]
        assert isinstance(raw_tags, list)
        assert isinstance(raw_dsp, list)
        tags = [str(tag) for tag in raw_tags]
        dsp = [str(operation) for operation in raw_dsp]
        source = {
            **copy.deepcopy(expected_source_metadata[source_id]),
            "source_sha256": gate._sha256(master),
        }
        sources.append(source)
        asset: dict[str, Any] = {
            "id": asset_id,
            "path": relative,
            "purpose": metadata["purpose"],
            "kind": kind,
            "tags": tags,
            "source_ids": [source_id],
            "sha256": gate._sha256(delivered),
            "format": dict(gate.FORMAT),
            "duration_target_sec": 1.0,
            "audio_quality": gate._audio_quality_evidence(
                delivered,
                quality_class="asset",
                target_lufs=gate.ASSET_TARGET_LUFS,
                tolerance_lu=gate.ASSET_TOLERANCE_LU,
            ),
            "license": metadata["license"],
            "layers": [
                {
                    "source_id": source_id,
                    "source_sha256": source["source_sha256"],
                    "source_start_sec": 0.0,
                    "duration_sec": 1.0,
                    "output_offset_sec": 0.0,
                    "gain_db": 0.0,
                    "dsp": dsp,
                    "license": metadata["license"],
                    "role": metadata["role"],
                }
            ],
        }
        upstream_artifact_id = gate._upstream_artifact_id_for_asset(asset_id)
        if upstream_artifact_id is not None:
            asset["upstream_lineage"] = gate._upstream_core_lineage(core_manifest, upstream_artifact_id)
        assets.append(asset)

    previews: list[dict[str, Any]] = []
    core_labels = {artifact_id: label for artifact_id, label in gate.CORE_PREVIEW_ARTIFACTS}
    for preview_id, (relative, group, expected_label, _render_required) in gate._expected_preview_contract().items():
        path = run_root / relative
        if group == "approved-core":
            if preview_id == "core.runtime-ad-mid":
                artifact_id = "cue.ad-mid"
                label = "Ad mid — runtime fit"
            else:
                artifact_id = preview_id.removeprefix("core.")
                label = core_labels[artifact_id]
            _write(path, core_paths[artifact_id].read_bytes())
        elif group == "compatibility":
            spec = next(item for item in gate.COMPATIBILITY_ASSETS if preview_id == f"preview.{item.asset_id}")
            label = spec.title
            assert path.is_file()
        elif preview_id == "preview.identity.time-check":
            label = "Time check — runtime sting"
            assert path.is_file()
        elif preview_id == "preview.bed.casa-notte-context":
            label = "Casa Notte — packaged fallback bed with approved voice"
            _fake_render_casa_preview(
                speech_paths["speech.spot-a"],
                pack_dir / gate.CASA_NOTTE.path,
                path,
            )
        elif preview_id.startswith("preview.recipe."):
            recipe_id = preview_id.removeprefix("preview.recipe.")
            definition_index = next(
                index for index, definition in enumerate(gate.RECIPE_DEFINITIONS) if definition.recipe_id == recipe_id
            )
            definition = gate.RECIPE_DEFINITIONS[definition_index]
            bed = next(
                item
                for item in gate.RECIPE_BEDS
                if item.asset_id.removeprefix("ad-bed.").replace("-", "_") == recipe_id
            )
            voice_id = "speech.spot-a" if definition_index % 2 == 0 else "speech.spot-b"
            _fake_render_recipe_preview(
                speech_paths[voice_id],
                pack_dir / bed.path,
                definition.bed_gain_db,
                (
                    (pack_dir / definition.cue_one.path, "intro", -13.0, definition.cue_one.duration_sec),
                    (pack_dir / definition.cue_two.path, "outro", -12.5, definition.cue_two.duration_sec),
                ),
                path,
            )
            label = recipe_id.replace("_", " ").title()
        else:
            raise AssertionError(f"Unhandled preview contract member: {preview_id}")
        assert label == expected_label
        preview: dict[str, Any] = {
            "id": preview_id,
            "label": label,
            "group": group,
            "path": relative,
            "sha256": gate._sha256(path),
            "duration_sec": 1.0,
            "format": dict(gate.FORMAT),
            "audio_quality": gate._audio_quality_evidence(
                path,
                quality_class="advertising-preview" if group == "advertising" else "program-preview",
                target_lufs=(
                    gate.ADVERTISING_PREVIEW_TARGET_LUFS if group == "advertising" else gate.PROGRAM_PREVIEW_TARGET_LUFS
                ),
                tolerance_lu=(
                    gate.ADVERTISING_PREVIEW_TOLERANCE_LU
                    if group == "advertising"
                    else gate.PROGRAM_PREVIEW_TOLERANCE_LU
                ),
            ),
        }
        if preview_id == "preview.bed.casa-notte-context":
            preview["render"] = gate._expected_casa_preview_render(speech_paths["speech.spot-a"])
        elif preview_id.startswith("preview.recipe."):
            preview["render"] = gate._expected_recipe_preview_render(
                definition,
                voice_id=voice_id,
                voice_path=speech_paths[voice_id],
                bed=bed,
            )
        previews.append(preview)

    raw_mid = pack_dir / "bumpers" / "ad_mid.mp3"
    mid_sha = gate._sha256(raw_mid)
    manifest = gate._build_manifest(
        generated_at=(datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ"),
        core_manifest_path=core_manifest_path,
        core_digest=core_digest,
        sources=sources,
        assets=assets,
        recipes=gate._expected_recipes(),
        previews=previews,
        mid_proof={
            "raw_pack_path": "bumpers/ad_mid.mp3",
            "raw_sha256": mid_sha,
            "helper": "mammamiradio.audio.imaging.ImagingLibrary.pick_ad_bumper",
            "duration_sec": 0.8,
            "target_lufs": -16.0,
            "fade_out_sec": 0.08,
            "runtime_output_sha256": gate._sha256(core_paths["cue.ad-mid"]),
            "approved_core_output_sha256": gate._sha256(core_paths["cue.ad-mid"]),
        },
    )
    _write(pack_dir / "ATTRIBUTION.md", b"fixture attribution\n")
    manifest_path = pack_dir / "manifest.json"
    result = Candidate(
        run_root=run_root,
        pack_dir=pack_dir,
        manifest_path=manifest_path,
        core_manifest_path=core_manifest_path,
        manifest=manifest,
        generator=generator,
        speech_paths=speech_paths,
        core_paths=core_paths,
        decisions_dir=tmp_path / "decisions",
        runtime_imaging_calls=runtime_imaging_calls,
    )
    result.save()
    return result


def test_casa_notte_preview_uses_runtime_imaging_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_dir = tmp_path / "pack"
    voice_path = _write(tmp_path / "speech" / "spot-a.mp3", b"voice")
    bed_path = _write(pack_dir / "beds" / "casa_notte.mp3", b"bed")
    output_path = tmp_path / "previews" / "casa.mp3"
    events: list[tuple[object, ...]] = []

    class FakeImagingLibrary:
        def __init__(
            self,
            playlists: list[object],
            work_dir: Path,
            *,
            bed_volume_db: float,
            assets_dir: Path,
            cache_dir: Path | None,
        ) -> None:
            events.append(("init", playlists, work_dir, bed_volume_db, assets_dir, cache_dir))

        def pick_talk_bed(self, duration: float, destination: Path) -> Path:
            events.append(("pick", duration, destination))
            return _write(destination, b"runtime-looped-bed")

    def fake_mix_voice_with_bed(
        voice: Path,
        bed: Path,
        output: Path,
        gain_db: float,
    ) -> Path:
        events.append(("mix", voice, bed, output, gain_db))
        return _write(output, b"runtime-casa-preview")

    def forbidden_normalize(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Casa Notte runtime preview must not call normalize_ad")

    monkeypatch.setattr(gate, "probe_duration_sec", lambda _path: 3.25)
    monkeypatch.setattr(gate, "ImagingLibrary", FakeImagingLibrary)
    monkeypatch.setattr(gate, "mix_voice_with_bed", fake_mix_voice_with_bed)
    monkeypatch.setattr(gate, "normalize_ad", forbidden_normalize)
    monkeypatch.setattr(gate._core, "_runtime_loudness_targets", lambda: nullcontext())

    assert gate._render_casa_notte_preview(voice_path, bed_path, output_path) == output_path

    assert [event[0] for event in events] == ["init", "pick", "mix"]
    assert events[0][1] == []
    assert events[0][3:] == (gate.TALK_BED_GAIN_DB, pack_dir, None)
    assert events[1][1] == 3.25
    assert events[2][1] == voice_path
    assert events[2][3:] == (output_path, gate.TALK_BED_GAIN_DB)
    assert output_path.read_bytes() == b"runtime-casa-preview"
    assert not list(output_path.parent.glob(".casa.*.work"))


def test_runtime_mid_uses_public_imaging_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_dir = tmp_path / "pack"
    output_path = tmp_path / "previews" / "ad_mid.mp3"
    events: list[tuple[object, ...]] = []

    class FakeRuntimeImaging:
        def pick_ad_bumper(
            self,
            destination: Path,
            *,
            duration_sec: float,
            role: str,
        ) -> Path:
            events.append(("pick_ad_bumper", destination, duration_sec, role))
            return _write(destination, b"runtime mid")

    def runtime_imaging(runtime_pack: Path, scratch_dir: Path) -> FakeRuntimeImaging:
        events.append(("runtime_imaging", runtime_pack, scratch_dir))
        return FakeRuntimeImaging()

    monkeypatch.setattr(gate, "_runtime_imaging_library", runtime_imaging)

    assert gate._render_runtime_mid_from_pack(pack_dir, output_path) == output_path
    assert events == [
        ("runtime_imaging", pack_dir, output_path.parent),
        ("pick_ad_bumper", output_path, 0.8, "mid"),
    ]


def test_generator_dependencies_are_the_exact_runtime_input_set() -> None:
    expected = {
        "scripts/complete_audio_pack_gate.py",
        "scripts/core_cadence_gate.py",
        "scripts/freeze_core_cadence_speech.py",
        "scripts/sonic_treatment_gate.py",
        "scripts/audition_tts_voices.py",
        "scripts/validate_audio_asset_pack.py",
        "mammamiradio/audio/admission.py",
        "mammamiradio/audio/imaging.py",
        "mammamiradio/audio/imaging_schema.py",
        "mammamiradio/audio/normalizer.py",
        "mammamiradio/audio/synth_cache.py",
        "mammamiradio/audio/tts.py",
        "mammamiradio/audio/voice_catalog.py",
        "mammamiradio/core/config.py",
        "mammamiradio/core/models.py",
        "mammamiradio/hosts/ad_creative.py",
        "mammamiradio/hosts/fallbacks.py",
        "mammamiradio/main.py",
        "mammamiradio/scheduling/producer.py",
        "radio.toml",
        "ha-addon/mammamiradio/radio.toml",
    }

    assert set(gate.GENERATOR_DEPENDENCIES) == expected
    assert len(gate.GENERATOR_DEPENDENCIES) == len(expected)


def test_runtime_audio_contract_accepts_both_exact_shipped_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert len(gate.EXPECTED_OFFICIAL_BRAND_CONTRACT) == 40
    assert gate.EXPECTED_OFFICIAL_BRAND_CONTRACT["Tappeto Testimone"] == ("home", "home_reveal")
    for relative_path in ("radio.toml", "ha-addon/mammamiradio/radio.toml"):
        _write(tmp_path / relative_path, _runtime_audio_toml())
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)

    gate._assert_runtime_audio_contract()


def test_runtime_audio_contract_rejects_recipe_set_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _runtime_audio_toml()
    for relative_path in ("radio.toml", "ha-addon/mammamiradio/radio.toml"):
        _write(tmp_path / relative_path, baseline)
    mutated = baseline.replace(b'sonic_recipe = "home_reveal"', b'sonic_recipe = "unknown_recipe"')
    _write(tmp_path / "radio.toml", mutated)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError, match="exact nine recipes"):
        gate._assert_runtime_audio_contract()


def test_runtime_audio_contract_rejects_brand_contract_drift_in_either_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _runtime_audio_toml()
    for relative_path in ("radio.toml", "ha-addon/mammamiradio/radio.toml"):
        _write(tmp_path / relative_path, baseline)
    mutated = baseline.replace(b'sonic_recipe = "cafe_testimonial"', b'sonic_recipe = "swap_placeholder"', 1)
    mutated = mutated.replace(b'sonic_recipe = "stadium_win"', b'sonic_recipe = "cafe_testimonial"', 1)
    mutated = mutated.replace(b'sonic_recipe = "swap_placeholder"', b'sonic_recipe = "stadium_win"', 1)
    _write(tmp_path / "ha-addon/mammamiradio/radio.toml", mutated)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError, match="canonical 40-brand map"):
        gate._assert_runtime_audio_contract()


def test_runtime_audio_contract_rejects_a_lockstep_brand_recipe_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _runtime_audio_toml()
    mutated = baseline.replace(
        b'name = "Prezzoforte"\ncategory = "supermarket"\nsonic_recipe = "supermarket_dash"',
        b'name = "Prezzoforte"\ncategory = "supermarket"\nsonic_recipe = "motorway_pass"',
        1,
    ).replace(
        b'name = "Velocino"\ncategory = "cars"\nsonic_recipe = "motorway_pass"',
        b'name = "Velocino"\ncategory = "cars"\nsonic_recipe = "supermarket_dash"',
        1,
    )
    assert mutated != baseline
    for relative_path in ("radio.toml", "ha-addon/mammamiradio/radio.toml"):
        _write(tmp_path / relative_path, mutated)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError, match="canonical 40-brand map"):
        gate._assert_runtime_audio_contract()


def test_runtime_audio_contract_binds_definitions_and_official_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _runtime_audio_toml()
    for relative_path in ("radio.toml", "ha-addon/mammamiradio/radio.toml"):
        _write(tmp_path / relative_path, baseline)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gate, "RECIPE_IDS", tuple(reversed(gate.RECIPE_IDS)))

    with pytest.raises(ValueError, match="definitions differ"):
        gate._assert_runtime_audio_contract()

    monkeypatch.setattr(gate, "RECIPE_IDS", tuple(definition.recipe_id for definition in gate.RECIPE_DEFINITIONS))
    monkeypatch.setattr(gate, "OFFICIAL_SONIC_RECIPE_IDS", frozenset({"unknown_recipe"}))
    with pytest.raises(ValueError, match="official runtime recipe registry"):
        gate._assert_runtime_audio_contract()


def test_short_generated_signals_remain_long_enough_for_finite_ebu_measurement() -> None:
    durations = {spec.asset_id: spec.duration_sec for spec in gate.COMPATIBILITY_ASSETS}
    definitions = {definition.recipe_id: definition for definition in gate.RECIPE_DEFINITIONS}

    assert durations["compat.ding"] == 0.5
    assert durations["compat.hotline-beep"] == 0.5
    assert definitions["late_night_hotline"].cue_one.duration_sec == 0.5


@pytest.mark.parametrize(
    ("measurement", "message"),
    [
        ((float("-inf"), -2.0), "non-finite"),
        ((-12.0, -2.0), "outside -16 ±2 LUFS"),
        ((-16.0, -0.5), "exceeds -1 dBTP"),
    ],
    ids=["non-finite", "loudness", "true-peak"],
)
def test_audio_quality_evidence_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    measurement: tuple[float, float],
    message: str,
) -> None:
    path = _write(tmp_path / "audio.mp3", b"audio")
    monkeypatch.setattr(gate._core, "_measure_loudness", lambda _path: measurement)

    with pytest.raises(ValueError, match=message):
        gate._audio_quality_evidence(path, quality_class="asset", target_lufs=-16.0, tolerance_lu=2.0)


@pytest.mark.parametrize(
    "relative_path",
    ["radio.toml", "ha-addon/mammamiradio/radio.toml"],
    ids=["root-config", "addon-config"],
)
@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"sample_rate = 48000", b"sample_rate = 44100"),
        (b"channels = 2", b"channels = 1"),
        (b"bitrate = 192", b"bitrate = 128"),
        (b"lufs_target = -16.0", b"lufs_target = -17.0"),
        (b"ad_lufs_target = -15.0", b"ad_lufs_target = -14.0"),
        (b"broadcast_chain = false", b"broadcast_chain = true"),
        (b"bed_volume_db = -18.0", b"bed_volume_db = -12.0"),
        (b'assets_dir = ""', b'assets_dir = "mammamiradio/assets/imaging"'),
    ],
    ids=[
        "sample-rate",
        "channels",
        "bitrate",
        "lufs-target",
        "ad-lufs-target",
        "broadcast-chain",
        "bed-gain",
        "assets-dir",
    ],
)
def test_runtime_audio_contract_rejects_drift_in_either_shipped_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    old: bytes,
    new: bytes,
) -> None:
    baseline = _runtime_audio_toml()
    for config_path in ("radio.toml", "ha-addon/mammamiradio/radio.toml"):
        _write(tmp_path / config_path, baseline)
    mutated = baseline.replace(old, new)
    assert mutated != baseline
    _write(tmp_path / relative_path, mutated)
    monkeypatch.setattr(gate, "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError, match="shipped runtime contract"):
        gate._assert_runtime_audio_contract()


def test_safe_output_roots_reject_outside_packaged_and_symlink_escape(tmp_path: Path) -> None:
    gate._require_safe_output_root(gate.REPO_ROOT / "tmp" / "complete-pack-test")
    gate._require_safe_output_root(Path(tempfile.gettempdir()) / "complete-pack-test")

    with pytest.raises(ValueError):
        gate._require_safe_output_root(gate.REPO_ROOT / "complete-pack-outside-tmp")
    with pytest.raises(ValueError):
        gate._require_safe_output_root(gate.REPO_ROOT / "mammamiradio" / "assets" / "imaging")

    escape = tmp_path / "escape"
    escape.symlink_to(gate.REPO_ROOT, target_is_directory=True)
    with pytest.raises(ValueError):
        gate._require_safe_output_root(escape / "candidate")


def test_valid_pending_candidate_validates_but_does_not_pass_listening_gate(candidate: Candidate) -> None:
    manifest, previews = gate.validate_complete_audio_pack(candidate.manifest_path)

    assert manifest["listening_receipt"]["status"] == "pending"
    assert set(previews) == {
        (candidate.run_root / relative).resolve()
        for relative, _group, _label, _render_required in gate._expected_preview_contract().values()
    }
    with pytest.raises(ValueError, match="not approved"):
        gate.assert_complete_listening_gate(candidate.manifest_path)


def test_board_audio_urls_are_content_addressed(candidate: Candidate) -> None:
    board = (candidate.run_root / "index.html").read_text(encoding="utf-8")

    for preview in candidate.manifest["previews"]:
        assert f"src='{preview['path']}?sha256={preview['sha256']}'" in board


@pytest.mark.parametrize(
    "mutation",
    ["wrong-label", "extra-field", "missing-render", "unexpected-render"],
    ids=["wrong-label", "extra-field", "missing-render", "unexpected-render"],
)
def test_preview_contract_is_exact_after_redigest(candidate: Candidate, mutation: str) -> None:
    if mutation == "wrong-label":
        candidate.preview("preview.recipe.cafe_testimonial")["label"] = "Pharmacy Whisper"
    elif mutation == "extra-field":
        candidate.preview("preview.recipe.cafe_testimonial")["claimed_identity"] = "pharmacy"
    elif mutation == "missing-render":
        candidate.preview("preview.recipe.cafe_testimonial").pop("render")
    else:
        candidate.preview("preview.compat.ding")["render"] = {"helper": "unreviewed"}
    candidate.save()

    with pytest.raises(ValueError, match=r"preview (?:has an invalid field set|label differs)"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize(
    ("record_kind", "record_id"),
    [
        ("asset", "compat.ding"),
        ("preview", "preview.recipe.cafe_testimonial"),
    ],
    ids=["asset", "preview"],
)
def test_live_audio_quality_evidence_must_match_digest_bound_record(
    candidate: Candidate,
    record_kind: str,
    record_id: str,
) -> None:
    record = candidate.asset(record_id) if record_kind == "asset" else candidate.preview(record_id)
    record["audio_quality"]["integrated_lufs"] = -99.0
    candidate.save()

    with pytest.raises(ValueError, match="loudness or true-peak evidence is stale"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_current_off_target_asset_fails_before_receipt(candidate: Candidate, monkeypatch: pytest.MonkeyPatch) -> None:
    target = (candidate.pack_dir / candidate.asset("compat.ding")["path"]).resolve()
    passing = gate._audio_quality_evidence

    def live_measurement(
        path: Path,
        *,
        quality_class: str,
        target_lufs: float,
        tolerance_lu: float,
    ) -> dict[str, object]:
        if path.resolve() == target:
            raise ValueError(f"Audio is outside -16 ±2 LUFS: {path} (-10.5)")
        return passing(
            path,
            quality_class=quality_class,
            target_lufs=target_lufs,
            tolerance_lu=tolerance_lu,
        )

    monkeypatch.setattr(gate, "_audio_quality_evidence", live_measurement)
    with pytest.raises(ValueError, match="outside -16 ±2 LUFS"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_delivered_asset_must_remain_identical_to_retained_source_master(candidate: Candidate) -> None:
    asset = candidate.asset("compat.ding")
    delivered = candidate.pack_dir / asset["path"]
    delivered.write_bytes(b"replacement-with-updated-manifest-evidence")
    asset["sha256"] = gate._sha256(delivered)
    candidate.save()

    with pytest.raises(ValueError, match="differs from its retained source master"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize("field", ["dsp", "role", "tags"], ids=["dsp", "role", "tags"])
def test_asset_authoring_metadata_is_exact(candidate: Candidate, field: str) -> None:
    asset = candidate.asset("compat.ding")
    if field == "dsp":
        asset["layers"][0]["dsp"] = ["unrelated-process"]
    elif field == "role":
        asset["layers"][0]["role"] = "texture"
    else:
        asset["tags"] = ["generic"]
    candidate.save()

    with pytest.raises(ValueError, match="provenance metadata differs from its authoring contract"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize(
    "mutation",
    ["asset-extra", "layer-extra", "unexpected-lineage", "missing-lineage"],
    ids=["asset-extra", "layer-extra", "unexpected-lineage", "missing-lineage"],
)
def test_asset_and_layer_field_sets_are_exact_after_redigest(candidate: Candidate, mutation: str) -> None:
    if mutation == "missing-lineage":
        candidate.asset("identity.station-id").pop("upstream_lineage")
    else:
        asset = candidate.asset("compat.ding")
        if mutation == "asset-extra":
            asset["claimed_origin"] = "unverified third-party sample"
        elif mutation == "layer-extra":
            asset["layers"][0]["copyright_owner"] = "unknown"
        else:
            asset["upstream_lineage"] = {}
    candidate.save()

    with pytest.raises(ValueError, match=r"asset(?: layer)? has an invalid field set"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_retained_source_master_path_is_exact(candidate: Candidate) -> None:
    asset = candidate.asset("compat.ding")
    source = next(item for item in candidate.manifest["sources"] if item["id"] == asset["source_ids"][0])
    original = candidate.pack_dir / source["original_path"]
    renamed = original.with_name("renamed-ding.mp3")
    original.rename(renamed)
    source["original_path"] = renamed.relative_to(candidate.pack_dir).as_posix()
    candidate.save()

    with pytest.raises(ValueError, match="source metadata differs from its authoring contract"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize(
    "field",
    ["title", "creator", "modification", "source_url", "generator"],
    ids=["title", "creator", "modification", "source-url", "generator-linkage"],
)
def test_retained_source_metadata_is_exact(candidate: Candidate, field: str) -> None:
    asset = candidate.asset("compat.ding")
    source = next(item for item in candidate.manifest["sources"] if item["id"] == asset["source_ids"][0])
    if field == "generator":
        source["generator"] = copy.deepcopy(source["generator"])
        source["generator"]["runtime"]["ffmpeg"] = "different generator evidence"
    else:
        source[field] = f"changed {field}"
    candidate.save()

    with pytest.raises(ValueError, match="source metadata differs from its authoring contract"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize("field", ["duration", "format"], ids=["duration", "format"])
def test_asset_live_format_and_duration_evidence_is_exact(candidate: Candidate, field: str) -> None:
    asset = candidate.asset("compat.ding")
    if field == "duration":
        asset["duration_target_sec"] = 1.05
        asset["layers"][0]["duration_sec"] = 1.05
    else:
        asset["format"] = {**gate.FORMAT, "bitrate_kbps": 128}
    candidate.save()

    with pytest.raises(ValueError, match="asset format or duration evidence is stale"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_validation_resolves_every_recipe_through_the_public_imaging_seam(
    candidate: Candidate,
) -> None:
    gate.validate_complete_audio_pack(candidate.manifest_path)

    assert [call for call in candidate.runtime_imaging_calls if call[0] == "resolve_ad_recipe"] == [
        ("resolve_ad_recipe", definition.recipe_id, f"complete-gate-{index}")
        for index, definition in enumerate(gate.RECIPE_DEFINITIONS)
    ]
    assert [call[0] for call in candidate.runtime_imaging_calls].count("pick_time_check_sting") == 1


def test_current_generator_dependency_hash_drift_invalidates_recorded_candidate(
    candidate: Candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_generator = copy.deepcopy(candidate.generator)
    dependency = next(
        item for item in current_generator["dependencies"] if item["path"] == "mammamiradio/audio/imaging.py"
    )
    dependency["sha256"] = "0" * 64
    monkeypatch.setattr(
        gate,
        "_pinned_generator_evidence",
        lambda: copy.deepcopy(current_generator),
    )

    with pytest.raises(ValueError, match="runtime dependencies changed"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize(
    "dependency_path",
    [
        "scripts/sonic_treatment_gate.py",
        "scripts/audition_tts_voices.py",
        "mammamiradio/audio/admission.py",
        "mammamiradio/audio/synth_cache.py",
        "mammamiradio/audio/voice_catalog.py",
        "mammamiradio/core/models.py",
        "mammamiradio/hosts/fallbacks.py",
    ],
    ids=[
        "sonic-treatment-gate",
        "tts-audition-gate",
        "ffmpeg-admission",
        "synth-cache",
        "voice-catalog",
        "core-models",
        "host-fallbacks",
    ],
)
def test_each_added_transitive_dependency_hash_drift_invalidates_recorded_candidate(
    candidate: Candidate,
    monkeypatch: pytest.MonkeyPatch,
    dependency_path: str,
) -> None:
    current_generator = copy.deepcopy(candidate.generator)
    dependency = next(item for item in current_generator["dependencies"] if item["path"] == dependency_path)
    dependency["sha256"] = "0" * 64
    monkeypatch.setattr(
        gate,
        "_pinned_generator_evidence",
        lambda: copy.deepcopy(current_generator),
    )

    with pytest.raises(ValueError, match="runtime dependencies changed"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_unrelated_revision_with_identical_dependency_hashes_keeps_candidate_valid(
    candidate: Candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_generator = copy.deepcopy(candidate.generator)
    current_generator["revision"] = "c" * 40
    current_generator["url"] = current_generator["url"].replace("a" * 40, "c" * 40)
    current_generator["runtime"] = {
        "python": "3.13.unrelated",
        "ffmpeg": "ffmpeg unrelated revision",
    }
    for dependency in current_generator["dependencies"]:
        dependency["url"] = dependency["url"].replace("a" * 40, "c" * 40)
    monkeypatch.setattr(
        gate,
        "_pinned_generator_evidence",
        lambda: copy.deepcopy(current_generator),
    )

    manifest, _previews = gate.validate_complete_audio_pack(candidate.manifest_path)

    assert manifest["content_digest"] == candidate.manifest["content_digest"]


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "wrong-path", "wrong-kind"])
def test_asset_inventory_is_exact(candidate: Candidate, mutation: str) -> None:
    assets = candidate.manifest["assets"]
    if mutation == "missing":
        assets.pop()
    elif mutation == "extra":
        extra_path = _write(candidate.pack_dir / "extra.mp3", b"extra")
        assets.append(
            {
                **assets[0],
                "id": "extra.asset",
                "path": "extra.mp3",
                "sha256": gate._sha256(extra_path),
            }
        )
    elif mutation == "duplicate":
        assets.append(dict(assets[0]))
    elif mutation == "wrong-path":
        asset = assets[-1]
        replacement = _write(candidate.pack_dir / "wrong.mp3", (candidate.pack_dir / asset["path"]).read_bytes())
        asset["path"] = "wrong.mp3"
        asset["sha256"] = gate._sha256(replacement)
    else:
        assets[-1]["kind"] = "cue"
    candidate.save()

    with pytest.raises(ValueError, match=r"asset inventory differs|path/kind contract differs"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_recipe_definitions_are_exact(candidate: Candidate) -> None:
    candidate.manifest["recipes"][0]["cues"][0]["gain_db"] = -99.0
    candidate.save()

    with pytest.raises(ValueError, match="exact nine recipe definitions"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_advertising_cues_require_unique_project_authored_foreground_sources(candidate: Candidate) -> None:
    first = candidate.asset("ad-cue.espresso_glint")
    second = candidate.asset("ad-cue.crowd_lift")
    first_source = next(source for source in candidate.manifest["sources"] if source["id"] == first["source_ids"][0])
    second["source_ids"] = list(first["source_ids"])
    second["layers"][0]["source_id"] = first["source_ids"][0]
    second["layers"][0]["source_sha256"] = first_source["source_sha256"]
    candidate.save()

    with pytest.raises(
        ValueError,
        match=r"foreground source appears in more than one advertising cue|provenance metadata differs",
    ):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize("mutation", ["texture", "second-layer"], ids=["wrong-role", "two-layers"])
def test_advertising_cues_require_exactly_one_foreground_layer(candidate: Candidate, mutation: str) -> None:
    cue = candidate.asset("ad-cue.espresso_glint")
    if mutation == "texture":
        cue["layers"][0]["role"] = "texture"
    else:
        cue["layers"].append(copy.deepcopy(cue["layers"][0]))
    candidate.save()

    with pytest.raises(
        ValueError,
        match=(
            r"exactly one foreground layer|lacks one project-authored foreground|"
            r"exact provenance layer|provenance metadata differs"
        ),
    ):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize("mutation", ["wrong-group", "duplicate", "wrong-path"])
def test_preview_inventory_is_exact(candidate: Candidate, mutation: str) -> None:
    previews = candidate.manifest["previews"]
    if mutation == "wrong-group":
        previews[0]["group"] = "advertising"
    elif mutation == "duplicate":
        previews.append(dict(previews[0]))
    else:
        preview = previews[-1]
        old_path = candidate.run_root / preview["path"]
        new_path = _write(candidate.run_root / "previews" / "wrong-path.mp3", old_path.read_bytes())
        preview["path"] = new_path.relative_to(candidate.run_root).as_posix()
    candidate.save()

    with pytest.raises(ValueError, match=r"preview id repeats|wrong exact preview inventory"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_upstream_core_digest_must_remain_exact(candidate: Candidate) -> None:
    candidate.manifest["upstream_core"]["pack_digest"] = "0" * 64
    candidate.save()

    with pytest.raises(ValueError, match="approved-core digest is stale"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize(
    ("asset_id", "artifact_id", "support_source_id"),
    [
        ("identity.station-id", "render.station-bed", "generated.identity-support.station"),
        ("identity.sweeper", "render.sweeper-bed", "generated.identity-support.sweeper"),
    ],
    ids=["station", "sweeper"],
)
def test_identity_assets_retain_all_three_approved_upstream_layers(
    candidate: Candidate,
    asset_id: str,
    artifact_id: str,
    support_source_id: str,
) -> None:
    lineage = candidate.asset(asset_id)["upstream_lineage"]
    upstream_artifact = next(
        artifact
        for artifact in gate._read_manifest(candidate.core_manifest_path)["artifacts"]
        if artifact["id"] == artifact_id
    )
    upstream_sources = gate._read_manifest(candidate.core_manifest_path)["sources"]
    referenced_ids = {layer["source_id"] for layer in upstream_artifact["layers"]}

    assert lineage["artifact"] == upstream_artifact
    assert len(lineage["artifact"]["layers"]) == 3
    assert [layer["source_id"] for layer in lineage["artifact"]["layers"]] == [
        "approved-treatment.signature",
        "approved-treatment.signature",
        support_source_id,
    ]
    assert lineage["sources"] == [source for source in upstream_sources if source["id"] in referenced_ids]


@pytest.mark.parametrize(
    ("asset_id", "artifact_id", "source_id"),
    [
        ("transition.music-to-speech", "cue.music-to-speech", "generated.transition.music-to-speech"),
        ("transition.speech-to-music", "cue.speech-to-music", "generated.transition.speech-to-music"),
        ("bumper.ad-in", "cue.ad-in", "generated.ad-marker.in"),
        ("bumper.ad-mid", "cue.ad-mid", "generated.ad-marker.mid"),
        ("bumper.ad-out", "cue.ad-out", "generated.ad-marker.out"),
    ],
    ids=["music-to-speech", "speech-to-music", "ad-in", "ad-mid", "ad-out"],
)
def test_transitions_and_bumpers_retain_original_source_ids_and_metadata(
    candidate: Candidate,
    asset_id: str,
    artifact_id: str,
    source_id: str,
) -> None:
    core_manifest = gate._read_manifest(candidate.core_manifest_path)
    upstream_artifact = next(artifact for artifact in core_manifest["artifacts"] if artifact["id"] == artifact_id)
    upstream_source = next(source for source in core_manifest["sources"] if source["id"] == source_id)
    lineage = candidate.asset(asset_id)["upstream_lineage"]

    assert lineage["artifact"]["layers"] == upstream_artifact["layers"]
    assert lineage["artifact"]["layers"][0]["source_id"] == source_id
    assert lineage["sources"] == [upstream_source]


@pytest.mark.parametrize(
    "mutation",
    ["drop-layer", "layer-dsp", "layer-license", "source-metadata"],
    ids=["drop-layer", "layer-dsp", "layer-license", "source-metadata"],
)
def test_upstream_core_lineage_tampering_fails_closed(candidate: Candidate, mutation: str) -> None:
    lineage = candidate.asset("identity.station-id")["upstream_lineage"]
    if mutation == "drop-layer":
        lineage["artifact"]["layers"].pop()
    elif mutation == "layer-dsp":
        lineage["artifact"]["layers"][0]["dsp"].append("unapproved:process")
    elif mutation == "layer-license":
        lineage["artifact"]["layers"][0]["license"] = "CC0-1.0"
    else:
        lineage["sources"][0]["upstream_metadata"]["fixture"] = False
    candidate.save()

    with pytest.raises(ValueError, match="upstream core lineage differs from approved artifact"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_delivered_core_asset_must_match_the_approved_artifact(candidate: Candidate) -> None:
    candidate.rewrite_asset("identity.station-id", b"redigested-but-not-approved-core")
    candidate.save()

    with pytest.raises(ValueError, match="core asset differs from approved artifact"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize(
    ("preview_id", "message"),
    [
        (f"core.{gate.CORE_PREVIEW_ARTIFACTS[0][0]}", "Approved core preview differs"),
        ("core.runtime-ad-mid", "runtime ad-mid preview differs"),
    ],
    ids=["approved-core", "runtime-ad-mid"],
)
def test_core_preview_bytes_must_match_the_proven_artifact(candidate: Candidate, preview_id: str, message: str) -> None:
    preview = candidate.preview(preview_id)
    preview_path = candidate.run_root / preview["path"]
    preview_path.write_bytes(b"redigested-but-not-proven-preview")
    preview["sha256"] = gate._sha256(preview_path)
    candidate.save()

    with pytest.raises(ValueError, match=message):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_recipe_preview_must_byte_regenerate_after_manifest_hash_is_refreshed(candidate: Candidate) -> None:
    preview = candidate.preview("preview.recipe.cafe_testimonial")
    preview_path = candidate.run_root / preview["path"]
    preview_path.write_bytes(b"tampered-but-redigested-recipe-preview")
    preview["sha256"] = gate._sha256(preview_path)
    candidate.save()

    with pytest.raises(ValueError, match=r"[Rr]ecipe.*preview|reproduce|regenerat"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_casa_preview_must_byte_regenerate_after_manifest_hash_is_refreshed(candidate: Candidate) -> None:
    preview = candidate.preview("preview.bed.casa-notte-context")
    preview_path = candidate.run_root / preview["path"]
    preview_path.write_bytes(b"tampered-but-redigested-casa-preview")
    preview["sha256"] = gate._sha256(preview_path)
    candidate.save()

    with pytest.raises(ValueError, match=r"Casa Notte.*runtime-faithful render"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize(
    ("preview_id", "message"),
    [
        ("preview.bed.casa-notte-context", "Casa Notte listening preview has stale runtime render evidence"),
        ("preview.recipe.cafe_testimonial", "Recipe preview has stale runtime render evidence"),
    ],
    ids=["casa", "recipe"],
)
def test_runtime_preview_render_metadata_is_exact(candidate: Candidate, preview_id: str, message: str) -> None:
    candidate.preview(preview_id)["render"]["bed_gain_db"] = -99.0
    candidate.save()

    with pytest.raises(ValueError, match=message):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_ad_mid_must_byte_regenerate_after_all_raw_hashes_are_refreshed(candidate: Candidate) -> None:
    raw_mid = candidate.pack_dir / "bumpers" / "ad_mid.mp3"
    raw_mid.write_bytes(b"tampered-raw-mid")
    asset = candidate.asset("bumper.ad-mid")
    asset["sha256"] = gate._sha256(raw_mid)
    source = next(item for item in candidate.manifest["sources"] if item["id"] == asset["source_ids"][0])
    master = (candidate.pack_dir / source["original_path"]).resolve()
    master.write_bytes(raw_mid.read_bytes())
    source["source_sha256"] = gate._sha256(master)
    asset["layers"][0]["source_sha256"] = source["source_sha256"]
    candidate.manifest["production_contract"]["mid_runtime_fit"]["raw_sha256"] = gate._sha256(raw_mid)
    candidate.save()

    with pytest.raises(ValueError, match="ad-mid does not reproduce"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_ad_mid_proof_rejects_the_stale_normalizer_helper(candidate: Candidate) -> None:
    candidate.manifest["production_contract"]["mid_runtime_fit"]["helper"] = (
        "mammamiradio.audio.normalizer.fit_audio_oneshot"
    )
    candidate.save()

    with pytest.raises(ValueError, match="ad-mid runtime proof has stale helper settings"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize("filename", ["index.html", "README.md"])
def test_static_board_files_are_integrity_bound(candidate: Candidate, filename: str) -> None:
    (candidate.run_root / filename).write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"listening-surface|ledger|page|handoff|board|README|index"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize("mode", ["missing", "stale", "symlink"])
def test_ready_marker_is_required_digest_bound_and_regular(candidate: Candidate, tmp_path: Path, mode: str) -> None:
    if mode == "missing":
        candidate.ready_path.unlink()
    elif mode == "stale":
        candidate.ready_path.write_text("0" * 64 + "\n", encoding="utf-8")
    else:
        target = tmp_path / "ready-target"
        target.write_text(str(candidate.manifest["content_digest"]) + "\n", encoding="utf-8")
        candidate.ready_path.unlink()
        candidate.ready_path.symlink_to(target)

    with pytest.raises((OSError, ValueError), match=r"\.ready|ready|marker|digest|symlink"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("preview", "preview must not be a symlink"),
        ("source", "source master must not be a symlink"),
        ("surface", "listening surface differs"),
    ],
)
def test_candidate_evidence_symlinks_are_rejected(
    candidate: Candidate, tmp_path: Path, kind: str, message: str
) -> None:
    if kind == "preview":
        raw_path = candidate.run_root / candidate.preview("preview.recipe.cafe_testimonial")["path"]
    elif kind == "source":
        source = candidate.manifest["sources"][0]
        raw_path = candidate.pack_dir / source["original_path"]
    else:
        raw_path = candidate.run_root / "index.html"
    target = _write(tmp_path / f"{kind}-symlink-target", raw_path.read_bytes())
    raw_path.unlink()
    raw_path.symlink_to(target)

    with pytest.raises(ValueError, match=message):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize(
    ("kind", "message"),
    [("preview", "preview escapes the board"), ("source", "source master escapes the promotable pack")],
)
def test_candidate_evidence_paths_cannot_escape(candidate: Candidate, tmp_path: Path, kind: str, message: str) -> None:
    outside = _write(tmp_path / f"outside-{kind}.mp3", b"outside evidence")
    if kind == "preview":
        preview = candidate.preview("preview.recipe.cafe_testimonial")
        preview["path"] = os.path.relpath(outside, candidate.run_root)
        preview["sha256"] = gate._sha256(outside)
    else:
        source = candidate.manifest["sources"][0]
        source["original_path"] = os.path.relpath(outside, candidate.pack_dir)
        source["source_sha256"] = gate._sha256(outside)
        asset = next(item for item in candidate.manifest["assets"] if item["source_ids"] == [source["id"]])
        asset["layers"][0]["source_sha256"] = source["source_sha256"]
    candidate.save()

    with pytest.raises(ValueError, match=message):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize("mode", ["extra", "missing", "empty-directory", "fifo"])
def test_board_file_inventory_is_exact(candidate: Candidate, mode: str) -> None:
    if mode == "extra":
        _write(candidate.run_root / "unexpected.txt", b"not part of the candidate")
    elif mode == "missing":
        (candidate.pack_dir / "ATTRIBUTION.md").unlink()
    elif mode == "empty-directory":
        (candidate.run_root / "unexpected-empty").mkdir()
    else:
        os.mkfifo(candidate.run_root / "unexpected.fifo")

    with pytest.raises(ValueError, match=r"board (?:directory )?inventory differs|special filesystem entries"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_evidence_snapshot_rejects_special_entries_and_records_directories(candidate: Candidate) -> None:
    snapshot = gate._complete_evidence_snapshot(candidate.manifest_path)
    assert snapshot["pack/"] == "directory"

    os.mkfifo(candidate.run_root / "concurrent.fifo")
    with pytest.raises(ValueError, match="special filesystem entries"):
        gate._complete_evidence_snapshot(candidate.manifest_path)


@pytest.mark.parametrize(
    "mutation",
    ["top-level-extra", "design-direction", "limitations"],
    ids=["top-level-extra", "design-direction", "limitations"],
)
def test_manifest_semantic_contract_is_exact_after_redigest(candidate: Candidate, mutation: str) -> None:
    if mutation == "top-level-extra":
        candidate.manifest["claimed_approval"] = True
        message = "invalid top-level field set"
    elif mutation == "design-direction":
        candidate.manifest["design_direction"]["signature"] = "rejected-trumpet-direction"
        message = "design direction differs"
    else:
        candidate.manifest["limitations"] = []
        message = "limitations differ"
    candidate.save()

    with pytest.raises(ValueError, match=message):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_manifest_semantic_change_without_new_content_digest_is_stale(candidate: Candidate) -> None:
    candidate.manifest["design_direction"]["signature"] = "changed-without-redigest"
    gate._write_json(candidate.manifest_path, candidate.manifest)

    with pytest.raises(ValueError, match="content digest differs"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_preview_label_drift_fails_before_a_stale_board_can_be_accepted(candidate: Candidate) -> None:
    candidate.manifest["previews"][0]["label"] = "Changed board-visible label"
    candidate.save(refresh_board=False)

    with pytest.raises(ValueError, match="preview label differs"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_generic_validator_and_attribution_seam_tracks_receipt_status(
    candidate: Candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, bool, object]] = []
    checked_reports: list[object] = []

    def validate(pack_dir: Path, *, require_listening_approval: bool = False) -> object:
        report = object()
        calls.append((pack_dir, require_listening_approval, report))
        return report

    monkeypatch.setattr(gate._validator, "validate_audio_asset_pack", validate)
    monkeypatch.setattr(gate._validator, "check_attribution", checked_reports.append)

    gate.validate_complete_audio_pack(candidate.manifest_path)
    candidate.approve()
    gate.validate_complete_audio_pack(candidate.manifest_path)
    candidate.manifest["release_ready"] = False
    rejected = candidate.manifest["listening_receipt"]
    rejected.update(
        {
            "status": "rejected",
            "approved_candidate_id": None,
            "small_speaker": "fail",
            "notes": "Rejected on the small speaker.",
        }
    )
    candidate.save()
    gate.validate_complete_audio_pack(candidate.manifest_path)

    assert [(pack_dir, required) for pack_dir, required, _report in calls] == [
        (candidate.pack_dir, False),
        (candidate.pack_dir, True),
        (candidate.pack_dir, False),
    ]
    assert checked_reports == [report for _pack_dir, _required, report in calls]


def test_pending_receipt_cannot_contain_decided_device_results(candidate: Candidate) -> None:
    candidate.manifest["listening_receipt"]["mac"] = "pass"
    gate._write_json(candidate.manifest_path, candidate.manifest)

    with pytest.raises(ValueError, match="Pending receipt has a decided mac result"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_approved_receipt_records_and_opens_the_digest_gate(candidate: Candidate) -> None:
    decision = candidate.write_decision()

    gate.record_complete_listening_receipt(candidate.manifest_path, decision)

    manifest, _previews = gate.validate_complete_audio_pack(candidate.manifest_path)
    assert manifest["release_ready"] is True
    assert manifest["listening_receipt"]["status"] == "approved"
    assert gate.assert_complete_listening_gate(candidate.manifest_path) == candidate.manifest["content_digest"]


def test_rejected_receipt_is_recorded_and_valid_but_never_opens_gate(candidate: Candidate) -> None:
    decision = candidate.write_decision(
        status="rejected",
        small_speaker="fail",
        notes="Too brittle on the small speaker.",
    )

    gate.record_complete_listening_receipt(candidate.manifest_path, decision)

    manifest, _previews = gate.validate_complete_audio_pack(candidate.manifest_path)
    assert manifest["release_ready"] is False
    assert manifest["listening_receipt"]["status"] == "rejected"
    with pytest.raises(ValueError, match="not approved"):
        gate.assert_complete_listening_gate(candidate.manifest_path)


def test_record_rejects_a_decision_for_a_stale_content_digest(candidate: Candidate) -> None:
    decision = candidate.write_decision()
    gate._write_json(
        decision,
        {
            "content_digest": "0" * 64,
            "status": "approved",
            "mac": "pass",
            "small_speaker": "pass",
            "sonos_arc": "pass",
            "notes": "This decision targets an older candidate.",
        },
    )
    original = candidate.manifest_path.read_bytes()

    with pytest.raises(ValueError, match="decision targets a stale content digest"):
        gate.record_complete_listening_receipt(candidate.manifest_path, decision)

    assert candidate.manifest_path.read_bytes() == original
    assert not candidate.receipt_lock_path.exists()


@pytest.mark.parametrize(
    ("status", "failed_field", "message"),
    [
        ("approved", "mac", "requires mac=pass"),
        ("approved", "small_speaker", "requires small_speaker=pass"),
        ("approved", "sonos_arc", "requires sonos_arc=pass"),
        ("rejected", None, "must identify a failed listening target"),
    ],
)
def test_receipt_decision_matrix_is_fail_closed(
    candidate: Candidate,
    status: str,
    failed_field: str | None,
    message: str,
) -> None:
    decision = candidate.write_decision(
        status=status,
        mac="fail" if failed_field == "mac" else "pass",
        small_speaker="fail" if failed_field == "small_speaker" else "pass",
        sonos_arc="fail" if failed_field == "sonos_arc" else "pass",
    )
    original = candidate.manifest_path.read_bytes()

    with pytest.raises(ValueError, match=message):
        gate.record_complete_listening_receipt(candidate.manifest_path, decision)

    assert candidate.manifest_path.read_bytes() == original
    assert not candidate.receipt_lock_path.exists()


def test_receipt_digest_binding_rejects_stale_manifest_receipt(candidate: Candidate) -> None:
    candidate.manifest["listening_receipt"]["content_digest"] = "0" * 64
    gate._write_json(candidate.manifest_path, candidate.manifest)

    with pytest.raises(ValueError, match="stale for the content digest"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


@pytest.mark.parametrize(
    "reviewed_at",
    [
        (datetime.now(UTC) - timedelta(days=365)).isoformat(),
        (datetime.now(UTC) + timedelta(days=365)).isoformat(),
    ],
    ids=["stale", "future"],
)
def test_approved_receipt_timestamp_must_be_current(candidate: Candidate, reviewed_at: str) -> None:
    candidate.approve(reviewed_at)

    with pytest.raises(ValueError, match=r"timestamp|stale|future|predates"):
        gate.validate_complete_audio_pack(candidate.manifest_path)


def test_existing_receipt_lock_preserves_pending_manifest(candidate: Candidate) -> None:
    decision = candidate.write_decision()
    original = candidate.manifest_path.read_bytes()
    lock_path = candidate.receipt_lock_path
    lock_path.write_text("held\n", encoding="utf-8")

    with pytest.raises(ValueError, match="already being decided"):
        gate.record_complete_listening_receipt(candidate.manifest_path, decision)

    assert candidate.manifest_path.read_bytes() == original
    assert lock_path.read_text(encoding="utf-8") == "held\n"


@pytest.mark.parametrize(
    ("mutation", "message", "expected_calls"),
    [
        ("manifest-before-write", "manifest changed while its decision was being recorded", 2),
        ("board-before-write", "board changed while its decision was being recorded", 2),
        ("board-after-write", "board changed while its decision was being committed", 3),
    ],
)
def test_receipt_record_aborts_on_concurrent_candidate_mutation(
    candidate: Candidate,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
    expected_calls: int,
) -> None:
    decision = candidate.write_decision()
    original = candidate.manifest_path.read_bytes()
    real_validate = gate.validate_complete_audio_pack
    calls = 0

    def validate_then_mutate(path: Path) -> tuple[dict[str, Any], tuple[Path, ...]]:
        nonlocal calls
        calls += 1
        result = real_validate(path)
        if calls == 2 and mutation == "manifest-before-write":
            path.write_bytes(path.read_bytes() + b"\n")
        elif (calls, mutation) in {(2, "board-before-write"), (3, "board-after-write")}:
            (candidate.run_root / "README.md").write_text("concurrent board change\n", encoding="utf-8")
        return result

    monkeypatch.setattr(gate, "validate_complete_audio_pack", validate_then_mutate)

    with pytest.raises(ValueError, match=message):
        gate.record_complete_listening_receipt(candidate.manifest_path, decision)

    assert calls == expected_calls
    assert gate._read_manifest(candidate.manifest_path)["listening_receipt"]["status"] == "pending"
    if mutation != "manifest-before-write":
        assert candidate.manifest_path.read_bytes() == original
    assert not candidate.receipt_lock_path.exists()


def test_failed_post_write_validation_rolls_back_and_removes_lock(
    candidate: Candidate, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = candidate.write_decision()
    original = candidate.manifest_path.read_bytes()
    calls = 0

    def fail_post_write_validation(_manifest_path: Path) -> tuple[dict[str, Any], tuple[Path, ...]]:
        nonlocal calls
        calls += 1
        if calls < 3:
            return candidate.manifest, ()
        raise ValueError("forced post-write validation failure")

    monkeypatch.setattr(gate, "validate_complete_audio_pack", fail_post_write_validation)

    with pytest.raises(ValueError, match="forced post-write"):
        gate.record_complete_listening_receipt(candidate.manifest_path, decision)

    assert candidate.manifest_path.read_bytes() == original
    assert calls == 3
    assert not candidate.receipt_lock_path.exists()
    assert not list(candidate.pack_dir.glob(".manifest.json.*.tmp"))
    assert not list(candidate.pack_dir.glob(".manifest.json.*.rollback"))


def test_manifest_symlink_is_rejected_before_resolution(candidate: Candidate) -> None:
    linked_manifest = candidate.run_root / "manifest-link.json"
    linked_manifest.symlink_to(candidate.manifest_path)

    with pytest.raises(ValueError, match="symlink"):
        gate.validate_complete_audio_pack(linked_manifest)


def test_receipt_record_rejects_manifest_symlink_before_resolution(candidate: Candidate) -> None:
    linked_manifest = candidate.run_root / "manifest-link.json"
    linked_manifest.symlink_to(candidate.manifest_path)

    with pytest.raises(ValueError, match="symlink"):
        gate.record_complete_listening_receipt(linked_manifest, candidate.write_decision())

    assert not candidate.receipt_lock_path.exists()


def test_render_writes_ready_before_validation_and_publishes_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "rendered-candidate"
    generated_at = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")
    content_digest = "d" * 64
    validated_staging: list[Path] = []

    def render(_core_manifest: Path, staging: Path, *, generated_at: str) -> dict[str, object]:
        gate._write_json(staging / "pack" / "manifest.json", {"content_digest": content_digest})
        return {"content_digest": content_digest, "generated_at": generated_at}

    def validate(manifest_path: Path) -> tuple[dict[str, Any], tuple[Path, ...]]:
        staging = manifest_path.parent.parent
        assert not output_root.exists()
        assert (staging / ".ready").read_text(encoding="utf-8") == f"{content_digest}\n"
        validated_staging.append(staging)
        return {"content_digest": content_digest}, ()

    monkeypatch.setattr(gate, "_render_complete_pack_in_place", render)
    monkeypatch.setattr(gate, "validate_complete_audio_pack", validate)

    manifest = gate.render_complete_audio_pack(
        tmp_path / "core.json",
        output_root,
        generated_at=generated_at,
    )

    assert manifest["content_digest"] == content_digest
    assert (output_root / ".ready").read_text(encoding="utf-8") == f"{content_digest}\n"
    assert len(validated_staging) == 1
    assert not validated_staging[0].exists()
    assert not list(tmp_path.glob(".rendered-candidate.*.staging"))


def test_render_never_clobbers_an_existing_output(tmp_path: Path) -> None:
    output_root = tmp_path / "rendered-candidate"
    marker = _write(output_root / "keep.txt", b"existing candidate")
    generated_at = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")

    with pytest.raises(FileExistsError, match="output already exists"):
        gate.render_complete_audio_pack(tmp_path / "core.json", output_root, generated_at=generated_at)

    assert marker.read_bytes() == b"existing candidate"
    assert not list(tmp_path.glob(".rendered-candidate.*.staging"))


def test_render_validation_failure_removes_staging_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_root = tmp_path / "rendered-candidate"
    generated_at = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")
    content_digest = "d" * 64

    def render(_core_manifest: Path, staging: Path, *, generated_at: str) -> dict[str, object]:
        assert generated_at == generated_at_value
        gate._write_json(staging / "pack" / "manifest.json", {"content_digest": content_digest})
        return {"content_digest": content_digest}

    def fail_validation(manifest_path: Path) -> tuple[dict[str, Any], tuple[Path, ...]]:
        assert (manifest_path.parent.parent / ".ready").is_file()
        raise RuntimeError("forced render failure")

    generated_at_value = generated_at
    monkeypatch.setattr(gate, "_render_complete_pack_in_place", render)
    monkeypatch.setattr(gate, "validate_complete_audio_pack", fail_validation)

    with pytest.raises(RuntimeError, match="forced render failure"):
        gate.render_complete_audio_pack(tmp_path / "core.json", output_root, generated_at=generated_at)

    assert not output_root.exists()
    assert not list(tmp_path.glob(".rendered-candidate.*.staging"))

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts import audition_sonic_brand as audition
from scripts import build_public_imaging_pack as pack_builder


def _write_pack(assets_dir: Path) -> None:
    for relative_path in audition.required_pack_paths():
        destination = assets_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"asset:{relative_path.as_posix()}".encode())


def _fake_render(sample: audition.SonicSample, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(f"baseline:{sample.key}".encode())
    return output_path


def test_required_pack_paths_covers_manifest_core_surfaces_and_sfx() -> None:
    required = {path.as_posix() for path in audition.required_pack_paths()}

    assert "manifest.json" in required
    assert "station_id.mp3" in required
    assert {"bumpers/ad_in.mp3", "bumpers/ad_mid.mp3", "bumpers/ad_out.mp3"} <= required
    assert "bumpers/ad_break.mp3" not in required
    assert "stingers/music_to_speech.mp3" in required
    assert "stingers/speech_to_music.mp3" in required
    assert "beds/casa_notte.mp3" in required
    assert {f"sfx/{name}.mp3" for name in audition.AVAILABLE_SFX_TYPES}.issubset(required)


def test_main_uses_deterministic_timestamp_and_writes_manifest_and_html(tmp_path, monkeypatch) -> None:
    assets_dir = tmp_path / "pack"
    _write_pack(assets_dir)
    monkeypatch.setattr(audition, "PACKAGED_ASSETS_DIR", assets_dir)
    monkeypatch.setattr(audition, "render_baseline_sample", _fake_render)

    output_dir = tmp_path / "auditions"
    timestamp = "20260710T120000Z"
    assert audition.main(["--output-dir", str(output_dir), "--timestamp", timestamp, "--no-recipe-previews"]) == 0

    run_dir = output_dir / f"audition-{timestamp}"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    page = (run_dir / "index.html").read_text()

    assert manifest["generated_at"] == timestamp
    assert manifest["mode"] == "local-only"
    assert manifest["pack"] == "Modern Night Drive — Neon Relay × Velvet Horizon"
    assert manifest["scene_recipes"] == []
    assert len(manifest["samples"]) == len(audition.SAMPLES)
    assert page.count("<audio controls") == len(audition.SAMPLES) * 2
    assert "Modern Night Drive packaged asset" in page
    assert "Neon Relay" in page
    assert "Velvet Horizon" in page
    assert "Recorded Night Drive" not in page
    assert "https://" not in page

    for result in manifest["samples"]:
        baseline = run_dir / result["baseline"]
        modern_night_drive = run_dir / result["modern_night_drive"]
        assert baseline.read_bytes() == f"baseline:{result['key']}".encode()
        assert modern_night_drive.read_bytes() == (assets_dir / result["source_asset"]).read_bytes()


def test_main_reports_invalid_timestamp_without_creating_output(tmp_path, monkeypatch, capsys) -> None:
    assets_dir = tmp_path / "pack"
    _write_pack(assets_dir)
    monkeypatch.setattr(audition, "PACKAGED_ASSETS_DIR", assets_dir)

    output_dir = tmp_path / "auditions"
    assert (
        audition.main(["--output-dir", str(output_dir), "--timestamp", "not-a-timestamp", "--no-recipe-previews"]) == 2
    )

    assert "timestamp must use YYYYMMDDTHHMMSSZ format" in capsys.readouterr().err
    assert not output_dir.exists()


def test_main_fails_clearly_when_the_modern_night_drive_pack_is_incomplete(tmp_path, monkeypatch, capsys) -> None:
    assets_dir = tmp_path / "pack"
    assets_dir.mkdir()
    monkeypatch.setattr(audition, "PACKAGED_ASSETS_DIR", assets_dir)

    output_dir = tmp_path / "auditions"
    assert audition.main(["--output-dir", str(output_dir), "--timestamp", "20260710T120000Z"]) == 2

    error = capsys.readouterr().err
    assert "Modern Night Drive pack is incomplete" in error
    assert "manifest.json" in error
    assert "station_id.mp3" in error
    assert "sfx/chime.mp3" in error
    assert not output_dir.exists()


def test_role_bumper_baselines_render_in_mid_and_out_independently(tmp_path, monkeypatch) -> None:
    calls: list[Path] = []

    def fake_bumper(output_path: Path) -> Path:
        calls.append(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"bumper")
        return output_path

    monkeypatch.setattr(audition, "generate_bumper_jingle", fake_bumper)
    role_samples = [sample for sample in audition.SAMPLES if sample.key in {"ad_in", "ad_mid", "ad_out"}]

    for sample in role_samples:
        audition.render_baseline_sample(sample, tmp_path / sample.output_name)

    assert [path.name for path in calls] == ["ad_in.mp3", "ad_mid.mp3", "ad_out.mp3"]


def test_render_audition_refuses_to_overwrite_a_prior_deterministic_run(tmp_path, monkeypatch) -> None:
    assets_dir = tmp_path / "pack"
    _write_pack(assets_dir)
    monkeypatch.setattr(audition, "render_baseline_sample", _fake_render)
    run_dir = tmp_path / "audition-20260710T120000Z"

    audition.render_audition(run_dir, assets_dir=assets_dir)

    try:
        audition.render_audition(run_dir, assets_dir=assets_dir)
    except FileExistsError as exc:
        assert "Refusing to overwrite" in str(exc)
    else:
        raise AssertionError("expected deterministic run collision to be rejected")


def test_motif_prototypes_retire_the_old_motif_and_exclude_rejected_instruments() -> None:
    candidates = pack_builder.MOTIF_PROTOTYPES
    assert [candidate.id for candidate in candidates] == [
        "midnight_signal",
        "city_pulse",
        "warm_resolve",
    ]
    assert all(0.8 <= candidate.duration_sec <= 1.2 for candidate in candidates)

    pitch_sequences = [
        [layer.pitch_semitones for layer in candidate.layers if layer.role == "foreground"] for candidate in candidates
    ]
    assert [0, 4, 7, 12] not in pitch_sequences
    assert pitch_sequences == [[7, 3, 0], [0, 5, 0], [0, 5, 9, 4]]

    declared_text = json.dumps(
        {
            "sources": [source.__dict__ for source in pack_builder.MOTIF_PROTOTYPE_SOURCES],
            "candidates": [candidate.__dict__ for candidate in candidates],
        },
        default=str,
    ).lower()
    assert "trumpet" not in declared_text
    assert "mandolin" not in declared_text


def test_motif_source_verification_fails_closed_on_any_hash_mismatch(tmp_path, monkeypatch) -> None:
    source = pack_builder.MOTIF_PROTOTYPE_SOURCES[0]
    altered = replace(source, filename="source.mp3", source_sha256=hashlib.sha256(b"expected").hexdigest())
    monkeypatch.setattr(pack_builder, "MOTIF_PROTOTYPE_SOURCES", (altered,))
    (tmp_path / altered.filename).write_bytes(b"altered")

    with pytest.raises(ValueError, match=r"SHA-256 mismatch for source\.mp3"):
        pack_builder.verify_motif_prototype_sources(tmp_path)


def test_rejected_recorded_pack_source_and_build_routes_are_disabled(tmp_path, capsys) -> None:
    with pytest.raises(RuntimeError, match="rejected Recorded Night Drive rebuild is retired"):
        pack_builder.verify_sources(tmp_path)
    with pytest.raises(RuntimeError, match="rejected Recorded Night Drive rebuild is retired"):
        pack_builder._render_asset(pack_builder.ASSETS[0], tmp_path, tmp_path / "output")
    with pytest.raises(RuntimeError, match="rejected Recorded Night Drive rebuild is retired"):
        pack_builder._manifest(tmp_path / "output")
    with pytest.raises(RuntimeError, match="rejected Recorded Night Drive rebuild is retired"):
        pack_builder.build(tmp_path, tmp_path / "output")

    assert pack_builder.main(["--source-dir", str(tmp_path)]) == 2
    assert "rejected Recorded Night Drive rebuild is retired" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()


def test_motif_manifest_records_exact_layers_digest_and_pending_receipt(tmp_path) -> None:
    for candidate in pack_builder.MOTIF_PROTOTYPES:
        (tmp_path / candidate.path).write_bytes(f"candidate:{candidate.id}".encode())

    manifest = pack_builder._motif_prototype_manifest(tmp_path, generated_at="20260813T171500Z")

    assert manifest["stage"] == "motif-selection"
    assert manifest["release_ready"] is False
    assert manifest["listening_receipt"] == {
        "status": "pending",
        "pack_digest": None,
        "approved_candidate_id": None,
        "surfaces": [],
        "device_classes": [],
        "reviewed_at": None,
    }
    assert len(manifest["pack_digest"]) == 64
    assert all(source["license"] == "CC0-1.0" and source["tags"] for source in manifest["sources"])
    for candidate in manifest["candidates"]:
        assert candidate["layers"]
        for layer in candidate["layers"]:
            assert set(layer) == {
                "source_id",
                "source_sha256",
                "source_start_sec",
                "duration_sec",
                "output_offset_sec",
                "gain_db",
                "dsp",
                "license",
                "role",
            }
            assert layer["role"] in {"foreground", "texture"}


def test_motif_gate_writes_durable_board_and_exact_listening_handoff(tmp_path, monkeypatch) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()

    def fake_render(_source_dir: Path, run_dir: Path, *, generated_at: str) -> dict[str, Any]:
        run_dir.mkdir(parents=True)
        candidates = []
        for candidate in pack_builder.MOTIF_PROTOTYPES:
            (run_dir / candidate.path).write_bytes(f"audio:{candidate.id}".encode())
            candidates.append(
                {
                    "id": candidate.id,
                    "label": candidate.label,
                    "brief": candidate.brief,
                    "path": candidate.path,
                    "sha256": hashlib.sha256(f"audio:{candidate.id}".encode()).hexdigest(),
                }
            )
        manifest: dict[str, Any] = {
            "generated_at": generated_at,
            "candidates": candidates,
            "sources": [
                {
                    "title": "Verified source",
                    "source_url": "https://example.test/source",
                    "creator": "Recorder",
                    "source_sha256": "a" * 64,
                }
            ],
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    monkeypatch.setattr(pack_builder, "render_motif_prototypes", fake_render)
    output_dir = tmp_path / "auditions"
    timestamp = "20260813T171500Z"

    assert (
        audition.main(
            [
                "--output-dir",
                str(output_dir),
                "--timestamp",
                timestamp,
                "--motif-source-dir",
                str(source_dir),
            ]
        )
        == 0
    )

    run_dir = output_dir / f"motif-gate-{timestamp}"
    page = (run_dir / "index.html").read_text()
    handoff = (run_dir / "README.md").read_text()
    assert page.count("<audio id=") == 3
    assert page.count('onclick="repeatThree') == 3
    assert "Prototype only" in page
    assert "Wohnzimmer Sonos Arc" in page
    assert "[Open the listening board](./index.html)" in handoff
    assert "[Midnight Signal](./midnight_signal.mp3)" in handoff
    assert "[City Pulse](./city_pulse.mp3)" in handoff
    assert "[Warm Resolve](./warm_resolve.mp3)" in handoff
    assert "https://example.test/source" in handoff
    assert audition.MOTIF_LISTENING_PROMPT in handoff


def test_treatment_gate_cli_routes_only_the_approved_identity_manifest(tmp_path, monkeypatch, capsys) -> None:
    identity_manifest = tmp_path / "identity" / "manifest.json"
    identity_manifest.parent.mkdir()
    identity_manifest.write_text("{}\n", encoding="utf-8")
    output_dir = tmp_path / "auditions"
    calls: list[tuple[Path, Path, str | None]] = []

    class TreatmentGateModule:
        @staticmethod
        def render_treatment_gate(
            manifest_path: Path,
            run_dir: Path,
            generated_at: str | None = None,
        ) -> dict[str, str]:
            calls.append((manifest_path, run_dir, generated_at))
            run_dir.mkdir(parents=True)
            return {"pack_digest": "a" * 64}

    monkeypatch.setattr(audition.importlib, "import_module", lambda _name: TreatmentGateModule)

    assert (
        audition.main(
            [
                "--output-dir",
                str(output_dir),
                "--timestamp",
                "20260815T203827Z",
                "--treatment-identity-manifest",
                str(identity_manifest),
            ]
        )
        == 0
    )

    run_dir = output_dir / "treatment-gate-20260815T203827Z"
    assert calls == [(identity_manifest, run_dir, "20260815T203827Z")]
    output = capsys.readouterr().out
    assert f"Treatment gate: {run_dir}" in output
    assert "Pack digest: " + "a" * 64 in output


def test_core_gate_cli_routes_only_the_approved_treatment_manifest(tmp_path, monkeypatch, capsys) -> None:
    treatment_manifest = tmp_path / "treatment" / "manifest.json"
    treatment_manifest.parent.mkdir()
    treatment_manifest.write_text("{}\n", encoding="utf-8")
    speech_manifest = tmp_path / "speech" / "manifest.json"
    speech_manifest.parent.mkdir()
    speech_manifest.write_text("{}\n", encoding="utf-8")
    output_dir = tmp_path / "auditions"
    calls: list[tuple[Path, Path, Path, str | None]] = []

    class CoreGateModule:
        @staticmethod
        def render_core_cadence_gate(
            manifest_path: Path,
            frozen_speech_manifest: Path,
            run_dir: Path,
            generated_at: str | None = None,
        ) -> dict[str, str]:
            calls.append((manifest_path, frozen_speech_manifest, run_dir, generated_at))
            run_dir.mkdir(parents=True)
            return {"pack_digest": "b" * 64}

    monkeypatch.setattr(audition.importlib, "import_module", lambda _name: CoreGateModule)

    assert (
        audition.main(
            [
                "--output-dir",
                str(output_dir),
                "--timestamp",
                "20260815T220000Z",
                "--core-treatment-manifest",
                str(treatment_manifest),
                "--core-speech-manifest",
                str(speech_manifest),
            ]
        )
        == 0
    )

    run_dir = output_dir / "core-cadence-gate-20260815T220000Z"
    assert calls == [(treatment_manifest, speech_manifest, run_dir, "20260815T220000Z")]
    output = capsys.readouterr().out
    assert f"Core cadence gate: {run_dir}" in output
    assert "Pack digest: " + "b" * 64 in output


def test_core_break_order_covers_no_mid_single_mid_and_successor_types(tmp_path) -> None:
    intro = tmp_path / "intro.mp3"
    ad_in = tmp_path / "in.mp3"
    ad_mid = tmp_path / "mid.mp3"
    ad_out = tmp_path / "out.mp3"
    outro = tmp_path / "outro.mp3"
    spot_one = tmp_path / "spot-one.mp3"
    spot_two = tmp_path / "spot-two.mp3"

    assert pack_builder.core_ad_break_parts(intro, [spot_one], ad_in, ad_out, outro, ad_mid_path=ad_mid) == [
        intro,
        ad_in,
        spot_one,
        ad_out,
        outro,
    ]
    assert pack_builder.core_ad_break_parts(
        intro,
        [spot_one, spot_two],
        ad_in,
        ad_out,
        outro,
        ad_mid_path=ad_mid,
    ) == [intro, ad_in, spot_one, ad_mid, spot_two, ad_out, outro]

    ad_break = tmp_path / "break.mp3"
    successor = tmp_path / "successor.mp3"
    handoff = tmp_path / "speech-to-music.mp3"
    dry_handoff = tmp_path / "music-to-speech.mp3"
    preroll = tmp_path / "preroll.mp3"
    assert pack_builder.core_cadence_parts(
        ad_break,
        successor,
        successor_kind="music",
        speech_to_music_path=handoff,
        predecessor_music_path=preroll,
    ) == [preroll, ad_break, handoff, successor]
    assert pack_builder.core_cadence_parts(
        ad_break,
        successor,
        successor_kind="speech",
        speech_to_music_path=handoff,
        predecessor_music_path=preroll,
    ) == [preroll, ad_break, successor]
    assert pack_builder.core_cadence_parts(
        ad_break,
        successor,
        successor_kind="music",
        music_to_speech_path=dry_handoff,
        speech_to_music_path=handoff,
        predecessor_music_path=preroll,
        predecessor_has_music_tail=False,
    ) == [preroll, dry_handoff, ad_break, handoff, successor]
    assert pack_builder.core_cadence_parts(
        ad_break,
        successor,
        successor_kind="speech",
        music_to_speech_path=dry_handoff,
        predecessor_music_path=preroll,
        predecessor_has_music_tail=False,
    ) == [preroll, dry_handoff, ad_break, successor]


def test_core_cues_have_distinct_break_foregrounds_and_signature_stays_outside_break() -> None:
    cues = {cue.id: cue for cue in pack_builder.CORE_AUDITION_CUES}
    foregrounds = [
        next(layer.source_id for layer in cues[cue_id].layers if layer.role == "foreground")
        for cue_id in ("core.ad-in", "core.ad-mid", "core.ad-out")
    ]

    assert len(set(foregrounds)) == 3
    assert foregrounds == [
        "radio-quantumriver-hq-preview",
        "epiano-kevinklang-hq-preview",
        "tape-trp-hq-preview",
    ]
    transition_foregrounds = [cue.source_id for cue in pack_builder.CORE_AUDITION_TRANSITIONS]
    assert transition_foregrounds == [
        "generated.transition-music-to-speech",
        "generated.transition-speech-to-music",
    ]
    assert len(set([*foregrounds, *transition_foregrounds])) == 5
    warm_pitch_sequence = [
        layer.pitch_semitones
        for layer in pack_builder._selected_motif("warm_resolve").layers
        if layer.role == "foreground"
    ]
    for cue in cues.values():
        assert [layer.pitch_semitones for layer in cue.layers if layer.role == "foreground"] != warm_pitch_sequence


def test_core_runtime_wrappers_delegate_to_canonical_audio_helpers(tmp_path, monkeypatch) -> None:
    calls: dict[str, tuple[Any, ...] | dict[str, Any]] = {}
    loudness_calls: list[tuple[float | None, float | None, dict[str, int]]] = []

    def fake_mix(voice: Path, sting: Path, output: Path) -> Path:
        calls["mix"] = (voice, sting, output)
        return output

    def fake_crossfade(music: Path, voice: Path, output: Path, **kwargs: Any) -> Path:
        calls["crossfade"] = (music, voice, output)
        calls["crossfade_kwargs"] = kwargs
        return output

    def fake_concat(paths: list[Path], output: Path, **kwargs: Any) -> Path:
        calls["concat"] = (*paths, output)
        calls["concat_kwargs"] = kwargs
        return output

    def fake_configure(main: float | None, ad: float | None, **kwargs: int) -> None:
        loudness_calls.append((main, ad, kwargs))

    monkeypatch.setattr(pack_builder, "mix_voice_with_sting", fake_mix)
    monkeypatch.setattr(pack_builder, "configure_loudness_reconcile", fake_configure)
    monkeypatch.setattr(pack_builder, "crossfade_voice_over_music", fake_crossfade)
    monkeypatch.setattr(pack_builder, "concat_files", fake_concat)
    voice = tmp_path / "voice.aiff"
    sting = tmp_path / "sting.mp3"
    music = tmp_path / "music.mp3"
    output = tmp_path / "output.mp3"

    pack_builder._render_runtime_voice_sting_mix(voice, sting, output)
    pack_builder._render_runtime_music_tail_intro(music, voice, output)
    pack_builder._concat_core_parts([music, voice], output, silence_ms=300)

    assert calls["mix"] == (voice, sting, output)
    assert loudness_calls == [
        (-16.0, -15.0, {"sample_rate": 48_000, "channels": 2, "bitrate": 192}),
        (None, None, {"sample_rate": 48_000, "channels": 2, "bitrate": 192}),
    ]
    assert calls["crossfade_kwargs"] == {
        "tail_seconds": 8.0,
        "voice_volume": 1.0,
        "music_fade_volume": 0.5,
        "voice_delay_ms": 150,
    }
    assert calls["concat_kwargs"] == {"silence_ms": 300, "loudnorm": False}


def test_core_audition_rejects_selected_signature_that_does_not_match_lineage(tmp_path, monkeypatch) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    selected_artifact = tmp_path / "warm_resolve.mp3"
    selected_artifact.write_bytes(b"changed-selected-audio")
    monkeypatch.setattr(pack_builder, "verify_motif_prototype_sources", lambda _path: None)

    output_root = tmp_path / "board"
    with pytest.raises(ValueError, match="artifact does not match selection lineage"):
        pack_builder.render_core_audition(
            source_root,
            output_root,
            selected_motif_id="warm_resolve",
            selected_motif_artifact=selected_artifact,
            generated_at="20260815T120000Z",
        )
    assert not output_root.exists()


def test_core_ad_mid_keeps_full_authored_duration_before_runtime_loop(tmp_path, monkeypatch) -> None:
    cue = next(cue for cue in pack_builder.CORE_AUDITION_CUES if cue.id == "core.ad-mid")
    foreground = next(layer for layer in cue.layers if layer.role == "foreground")
    assert foreground.output_offset_sec == 0.0

    collapsed_render = tmp_path / "ad_mid.mp3"
    collapsed_render.write_bytes(b"collapsed")
    monkeypatch.setattr(pack_builder, "_probe_duration_sec", lambda _path: 0.048)

    with pytest.raises(RuntimeError, match=r"core\.ad-mid decoded duration is 0\.048s"):
        pack_builder._require_rendered_duration(
            collapsed_render,
            minimum_sec=cue.duration_sec - 0.05,
            render_id=cue.id,
        )


def test_core_audition_builds_five_pending_previews_without_touching_shipped_pack(tmp_path, monkeypatch) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    output_root = tmp_path / "board"
    selected_artifact = tmp_path / "warm_resolve.mp3"
    selected_artifact.write_bytes(b"motif:warm_resolve")
    calls: dict[str, list[Any]] = {"identity_beds": [], "loops": [], "concats": []}

    def write_audio(path: Path, marker: str | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((marker or path.as_posix()).encode())
        return path

    def fake_motif(spec, _source_root: Path, output: Path) -> Path:
        return write_audio(output / spec.path, f"motif:{spec.id}")

    def fake_speech(spec, output: Path, **_kwargs: Any) -> dict[str, Any]:
        write_audio(output, f"speech:{spec.id}")
        return {
            "id": spec.id,
            "license": "audition-only-local-generation",
            "creator": "fixture",
            "title": spec.purpose,
            "artifact_path": f"sources/{output.name}",
            "source_sha256": hashlib.sha256(f"speech:{spec.id}".encode()).hexdigest(),
            "duration_sec": 1.0,
            "origin": {"kind": "fixture", "declared_text": spec.text},
            "tags": ["speech", "italian", "audition-only"],
        }

    def fake_identity(_signature: Path, _tape: Path, output: Path, **kwargs: Any) -> Path:
        calls["identity_beds"].append(kwargs)
        return write_audio(output)

    def fake_loop(source: Path, output: Path, duration: float, **kwargs: Any) -> Path:
        calls["loops"].append((source, output, duration, kwargs))
        return write_audio(output)

    def fake_concat(paths: list[Path], output: Path, *, silence_ms: int) -> Path:
        calls["concats"].append(([path.name for path in paths], silence_ms))
        return write_audio(output)

    monkeypatch.setattr(pack_builder, "verify_motif_prototype_sources", lambda _path: None)
    monkeypatch.setattr(pack_builder, "_render_motif_prototype", fake_motif)
    monkeypatch.setitem(
        pack_builder.MOTIF_PROTOTYPE_SHA256,
        "warm_resolve",
        hashlib.sha256(b"motif:warm_resolve").hexdigest(),
    )
    monkeypatch.setattr(pack_builder, "_render_core_speech_source", fake_speech)
    monkeypatch.setattr(pack_builder, "_render_identity_role_bed", fake_identity)
    monkeypatch.setattr(
        pack_builder,
        "_render_runtime_voice_sting_mix",
        lambda _voice, _sting, output: write_audio(output),
    )
    monkeypatch.setattr(pack_builder, "_render_core_cue", fake_motif)
    monkeypatch.setattr(
        pack_builder,
        "_render_generated_core_cue",
        lambda cue, output_root: write_audio(output_root / cue.path),
    )
    monkeypatch.setattr(pack_builder, "fit_audio_oneshot", fake_loop)
    monkeypatch.setattr(
        pack_builder,
        "_render_context_music",
        lambda output, **_kwargs: write_audio(output),
    )
    monkeypatch.setattr(
        pack_builder,
        "_render_music_preroll",
        lambda _music, output: write_audio(output),
    )
    monkeypatch.setattr(
        pack_builder,
        "_render_runtime_music_tail_intro",
        lambda _music, _voice, output: write_audio(output),
    )
    monkeypatch.setattr(
        pack_builder,
        "_render_representative_spot",
        lambda _voice, _tape, output: write_audio(output),
    )
    monkeypatch.setattr(pack_builder, "_render_dry_voice", lambda _voice, output: write_audio(output))
    monkeypatch.setattr(pack_builder, "_concat_core_parts", fake_concat)
    monkeypatch.setattr(
        pack_builder,
        "_probe_duration_sec",
        lambda path: 12.0 if path.name == "program_music_tail.mp3" else 1.0,
    )

    manifest = pack_builder.render_core_audition(
        source_root,
        output_root,
        selected_motif_id="warm_resolve",
        selected_motif_artifact=selected_artifact,
        generated_at="20260815T120000Z",
    )

    assert [preview["id"] for preview in manifest["previews"]] == [
        "station-id",
        "sweeper",
        "ad-in",
        "ad-out",
        "full-cadence",
    ]
    assert manifest["motif_selection"] == {
        "schema_version": 1,
        "status": "selected",
        "candidate_id": "warm_resolve",
        "prototype_pack_digest": pack_builder.MOTIF_PROTOTYPE_PACK_DIGEST,
        "candidate_sha256": pack_builder.MOTIF_PROTOTYPE_SHA256["warm_resolve"],
        "recorded_at": "20260815T120000Z",
    }
    assert manifest["selected_motif_label"] == "Warm Resolve"
    assert manifest["previews"][0]["purpose"].startswith("Warm Resolve")
    assert manifest["release_ready"] is False
    assert manifest["listening_receipt"]["status"] == "pending"
    assert next(speech for speech in pack_builder.CORE_AUDITION_SPEECH if speech.id == "speech.station-id").text == (
        "Mamma Mi Radio... da Windor a Vergen, la voce che non si spegne mai!"
    )
    renders = {render["id"]: render for render in manifest["internal_renders"]}
    assert renders["render.station-bed"]["layers"][-1]["duration_sec"] == 2.28
    assert renders["render.sweeper-bed"]["layers"][-1]["duration_sec"] == 1.28
    preroll_layer = renders["render.program-music-preroll"]["layers"][0]
    assert preroll_layer["source_start_sec"] == 9.5
    assert preroll_layer["duration_sec"] == 2.5
    assert "render.ad-intro" in renders
    assert "render.representative-spot-two" in renders
    declared_layer_sources = {
        *(source["id"] for source in manifest["sources"]),
        *(render["id"] for render in manifest["internal_renders"]),
    }
    for record in [*manifest["previews"], *manifest["internal_renders"]]:
        for layer in record.get("layers", []):
            assert layer["source_id"] in declared_layer_sources
    reachable_sources = {layer["source_id"] for preview in manifest["previews"] for layer in preview.get("layers", [])}
    while True:
        expanded = reachable_sources | {
            layer["source_id"]
            for render_id in reachable_sources & renders.keys()
            for layer in renders[render_id].get("layers", [])
        }
        if expanded == reachable_sources:
            break
        reachable_sources = expanded
    assert renders.keys() <= reachable_sources
    assert calls["identity_beds"] == [
        {"duration_sec": 3.0, "pad_hz": 110.0},
        {"duration_sec": 2.0, "pad_hz": 146.83},
    ]
    assert calls["loops"][0][2:] == (
        0.8,
        {"target_lufs": -16.0, "fade_out_sec": 0.08},
    )
    assert calls["concats"][0][1] == 300
    assert calls["concats"][1] == (
        [
            "program_music_preroll.mp3",
            "music_to_speech.mp3",
            "ad_break.mp3",
            "speech_to_music.mp3",
            "program_music_successor.mp3",
        ],
        0,
    )
    assert (output_root / "README.md").is_file()
    readme = (output_root / "README.md").read_text()
    page = (output_root / "index.html").read_text()
    assert "Selected direction: Warm Resolve (`warm_resolve`)" in readme
    assert "Warm Resolve is direction-selected" in page
    assert page.count("<audio controls") == 5


@pytest.mark.requires_ffmpeg
def test_scene_recipe_previews_cover_the_shipped_recorded_recipe_inventory(tmp_path) -> None:
    results = audition.render_recipe_previews(tmp_path, assets_dir=audition.PACKAGED_ASSETS_DIR)

    assert {result.id for result in results} == {
        "bureaucracy_stamp",
        "cafe_testimonial",
        "home_reveal",
        "late_night_hotline",
        "motorway_pass",
        "pharmacy_whisper",
        "showroom_reveal",
        "stadium_win",
        "supermarket_dash",
    }
    assert all((tmp_path / result.audio).is_file() for result in results)
    assert all(len(result.cues) <= 2 for result in results)

"""Release guards for the browser-served First Listen narration pack."""

from __future__ import annotations

import argparse
import ast
import asyncio
import errno
import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = ROOT / "scripts" / "validate-spoken-assets.py"
GENERATOR_PATH = ROOT / "scripts" / "generate-first-listen-guide.py"
SHIPPED_AUDIO_ROOT = ROOT / "mammamiradio" / "web" / "static" / "audio"
EXPECTED_CLIPS = (
    "first_listen/welcome.mp3",
    "first_listen/sound-check.mp3",
    "first_listen/not-yet.mp3",
    "first_listen/receipt-recovery.mp3",
    "first_listen/privacy.mp3",
    "first_listen/ai.mp3",
    "first_listen/success.mp3",
)


def _load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_script("validate_spoken_assets", VALIDATOR_PATH)
GENERATOR = _load_script("generate_first_listen_guide", GENERATOR_PATH)


@pytest.fixture
def copied_pack(tmp_path: Path) -> tuple[Path, Path]:
    static_root = tmp_path / "static"
    audio_root = static_root / "audio"
    shutil.copytree(SHIPPED_AUDIO_ROOT, audio_root)
    return static_root, audio_root


@pytest.fixture
def stub_browser_media_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep schema/metadata tests independent of host FFmpeg packages."""

    monkeypatch.setattr(VALIDATOR.shutil, "which", lambda command: f"/test-bin/{command}")

    def probe(path: Path, *, ffprobe: str):
        del ffprobe
        manifest = json.loads((path.parents[1] / "spoken_assets.json").read_text(encoding="utf-8"))
        relative_path = f"first_listen/{path.name}"
        entry = next(item for item in manifest["assets"] if item["path"] == relative_path)
        return (
            {
                "stream": {
                    "codec_type": "audio",
                    "codec_name": "mp3",
                    "sample_rate": "48000",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "bit_rate": "192000",
                },
                "format": {"duration": str(entry["duration_seconds"])},
            },
            None,
        )

    monkeypatch.setattr(VALIDATOR, "_probe_audio", probe)
    monkeypatch.setattr(VALIDATOR, "_measure_loudness", lambda path, *, ffmpeg: ((-16.0, -2.0), None))


def _rewrite_manifest(audio_root: Path, mutate) -> None:
    manifest_path = audio_root / "spoken_assets.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.requires_ffmpeg
def test_shipped_browser_narration_pack_is_complete_playable_and_bounded() -> None:
    assert VALIDATOR.BROWSER_GUIDE_PATHS == EXPECTED_CLIPS
    assert VALIDATOR.validate_browser_narration_pack() == []


def test_shipped_canonical_receipt_matches_generator_and_current_radio_config() -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    config = GENERATOR._load_station_config(ROOT / "radio.toml")
    hosts = {host.name: host for host in config.hosts if host.name in GENERATOR.CANONICAL_HOST_NAMES}

    receipt = manifest["canonical_render_receipt"]

    assert manifest["render_provider"] == "canonical"
    assert receipt == GENERATOR._canonical_render_receipt(hosts)
    assert receipt == VALIDATOR._canonical_render_receipt()
    assert receipt["fallback"] is False
    assert [host["voice_id"] for host in receipt["hosts"]] == [
        "o4b57JYAECRMJyCEXyIE",
        "fNmw8sukfGuvWVOp33Ge",
    ]


def test_v3_numeric_guard_uses_python39_safe_isinstance_tuple() -> None:
    assert VALIDATOR._canonical_voice_settings(
        host_name="Marco",
        model_id=VALIDATOR._ELEVENLABS_V3_MODEL,
        raw_settings={"stability": 0.5},
    ) == {"stability": 0.5}

    source = textwrap.dedent(inspect.getsource(VALIDATOR._canonical_voice_settings))
    tree = ast.parse(source)
    stability_checks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "isinstance"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "stability"
    ]
    assert len(stability_checks) == 2
    numeric_check = stability_checks[1]
    assert isinstance(numeric_check.args[1], ast.Name)
    assert numeric_check.args[1].id == "_PY39_NUMERIC_TYPES"
    numeric_type_assignment = next(
        node
        for node in ast.parse(VALIDATOR_PATH.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_PY39_NUMERIC_TYPES" for target in node.targets)
    )
    assert isinstance(numeric_type_assignment.value, ast.Tuple)


def test_generator_selected_env_precedes_runtime_import_without_overriding_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "render.env"
    env_file.write_text("ELEVENLABS_API_KEY=selected-file-key\n", encoding="utf-8")

    monkeypatch.setenv("ELEVENLABS_API_KEY", "shell-key")
    GENERATOR._load_environment(env_file)
    assert os.environ["ELEVENLABS_API_KEY"] == "shell-key"

    monkeypatch.delenv("ELEVENLABS_API_KEY")
    observed: list[str | None] = []

    class StopBeforeRuntimeCallsError(RuntimeError):
        pass

    def stop_at_config(_path: Path):
        observed.append(os.getenv("ELEVENLABS_API_KEY"))
        raise StopBeforeRuntimeCallsError

    monkeypatch.setattr(GENERATOR, "_load_station_config", stop_at_config)
    args = argparse.Namespace(
        env_file=env_file,
        output_root=tmp_path / "audio",
    )

    with pytest.raises(StopBeforeRuntimeCallsError):
        asyncio.run(GENERATOR._run(args))

    assert observed == ["selected-file-key"]


def test_generator_has_no_top_level_runtime_imports_before_env_selection() -> None:
    tree = ast.parse(GENERATOR_PATH.read_text(encoding="utf-8"))
    runtime_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        and (
            (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mammamiradio"))
            or (isinstance(node, ast.Import) and any(alias.name.startswith("mammamiradio") for alias in node.names))
        )
    ]

    assert runtime_imports == []


def test_generator_cli_keeps_staging_inputs_and_rejects_provider_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "staged-audio"
    env_file = tmp_path / "render.env"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate-first-listen-guide.py",
            "--output-root",
            str(output_root),
            "--env-file",
            str(env_file),
        ],
    )

    args = GENERATOR._arguments()

    assert args.output_root == output_root
    assert args.env_file == env_file
    assert not hasattr(args, "provider")

    monkeypatch.setattr(sys, "argv", ["generate-first-listen-guide.py", "--provider", "edge"])
    with pytest.raises(SystemExit) as exc_info:
        GENERATOR._arguments()
    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_generator_render_line_preserves_v3_canonical_voice_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mammamiradio.audio.tts as tts
    from mammamiradio.core.models import HostPersonality

    output_path = tmp_path / "marco-v3.mp3"
    host = HostPersonality(
        name="Marco",
        voice="marco-v3-voice",
        style="test",
        engine="elevenlabs",
        edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
        voice_settings={"stability": 0.6},
        elevenlabs_model="eleven_v3",
        delivery_profile="marco",
    )
    calls: list[tuple[str, str, Path, dict[str, object]]] = []

    async def fake_synthesize_elevenlabs(text: str, voice: str, destination: Path, **kwargs):
        calls.append((text, voice, destination, kwargs))
        return destination

    async def reject_generic_synthesize(*_args, **_kwargs):
        raise AssertionError("First Listen guide rendering must never use a fallback-capable TTS route")

    monkeypatch.setattr(tts, "synthesize_elevenlabs", fake_synthesize_elevenlabs)
    monkeypatch.setattr(tts, "synthesize", reject_generic_synthesize)

    result = await GENERATOR._render_line(host, "Siamo in onda!", output_path)

    assert result == output_path
    assert calls == [
        (
            "Siamo in onda!",
            "marco-v3-voice",
            output_path,
            {
                "loudnorm": False,
                "voice_settings": {"stability": 0.6},
                "elevenlabs_model": "eleven_v3",
                "delivery_profile": "marco",
                "host_name": "Marco",
            },
        )
    ]


def test_generator_scratch_does_not_require_repo_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "fresh-checkout"
    repo_root.mkdir()
    monkeypatch.setattr(GENERATOR, "REPO_ROOT", repo_root)
    assert not (repo_root / "tmp").exists()

    with GENERATOR._temporary_work_directory() as work_dir:
        assert Path(work_dir).is_dir()


def test_generator_print_path_supports_external_output_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "checkout"
    external_manifest = tmp_path / "external" / "spoken_assets.json"
    monkeypatch.setattr(GENERATOR, "REPO_ROOT", repo_root)

    assert GENERATOR._display_path(external_manifest) == str(external_manifest)


def test_generator_publish_handles_cross_filesystem_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / "os-temp" / "welcome.mp3"
    destination = tmp_path / "external-output" / "welcome.mp3"
    staged.parent.mkdir()
    destination.parent.mkdir()
    staged.write_bytes(b"rendered audio")
    real_rename = os.rename

    def cross_device_rename(source, target):
        if Path(source) == staged:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_rename(source, target)

    monkeypatch.setattr(os, "rename", cross_device_rename)

    GENERATOR._publish_staged_file(staged, destination)

    assert destination.read_bytes() == b"rendered audio"
    assert not staged.exists()


def test_admin_guide_metadata_matches_shipped_manifest() -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))

    assert VALIDATOR._validate_admin_guide_metadata(manifest) == []


def test_admin_guide_metadata_rejects_hash_and_transcript_drift(tmp_path: Path) -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    source = source.replace("version:'b184b5d50c6e'", "version:'000000000000'", 1)
    source = source.replace("Five small steps, and Mamma Mi Radio is in your room.", "This copy drifted.", 1)
    template_path = tmp_path / "admin.html"
    template_path.write_text(source, encoding="utf-8")

    errors = VALIDATOR._validate_admin_guide_metadata(manifest, admin_template_path=template_path)

    assert any("admin guide welcome version" in error and "manifest sha256 prefix" in error for error in errors)
    assert "admin guide welcome transcript does not match spoken_assets.json" in errors


def test_admin_guide_metadata_rejects_container_inventory_drift(tmp_path: Path) -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    source = source.replace('class="guide-audio" data-guide="welcome"', 'class="guide-audio" data-guide="extra"', 1)
    template_path = tmp_path / "admin.html"
    template_path.write_text(source, encoding="utf-8")

    errors = VALIDATOR._validate_admin_guide_metadata(manifest, admin_template_path=template_path)

    assert "admin guide containers are missing: welcome" in errors
    assert "admin guide containers have unexpected keys: extra" in errors


def test_admin_guide_metadata_rejects_button_key_and_onclick_mismatch(tmp_path: Path) -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    source = source.replace('data-guide-key="welcome"', 'data-guide-key="sound-check"', 1)
    source = source.replace(
        "onclick=\"toggleFirstListenGuide('welcome',this)\"",
        "onclick=\"toggleFirstListenGuide('sound-check',this)\"",
        1,
    )
    template_path = tmp_path / "admin.html"
    template_path.write_text(source, encoding="utf-8")

    errors = VALIDATOR._validate_admin_guide_metadata(manifest, admin_template_path=template_path)

    assert "admin guide welcome play button data-guide-key 'sound-check' does not match its container" in errors
    assert any(
        "admin guide welcome play button onclick" in error and "toggleFirstListenGuide('welcome',this)" in error
        for error in errors
    )


def test_admin_guide_metadata_rejects_unrecognized_button_key(tmp_path: Path) -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    source = source.replace('data-guide-key="welcome"', 'data-guide-key="unknown"', 1)
    template_path = tmp_path / "admin.html"
    template_path.write_text(source, encoding="utf-8")

    errors = VALIDATOR._validate_admin_guide_metadata(manifest, admin_template_path=template_path)

    assert "admin guide welcome play button has unrecognized data-guide-key 'unknown'" in errors


def test_admin_guide_metadata_requires_exactly_one_play_button_per_container(tmp_path: Path) -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    welcome_button = (
        '<button type="button" class="guide-audio-play" data-guide-key="welcome" '
        'aria-describedby="guideWelcomeNote" onclick="toggleFirstListenGuide(\'welcome\',this)">'
        "Hear Marco and Giulia</button>"
    )
    assert source.count(welcome_button) == 1
    source = source.replace(welcome_button, f"{welcome_button}{welcome_button}", 1)
    source = source.replace(
        'class="guide-audio-play" data-guide-key="sound-check"',
        'class="guide-audio-play-disabled" data-guide-key="sound-check"',
        1,
    )
    template_path = tmp_path / "admin.html"
    template_path.write_text(source, encoding="utf-8")

    errors = VALIDATOR._validate_admin_guide_metadata(manifest, admin_template_path=template_path)

    assert "admin guide welcome must contain exactly one guide-audio-play button; found 2" in errors
    assert "admin guide sound-check must contain exactly one guide-audio-play button; found 0" in errors


def test_default_validates_both_inventories_and_custom_root_stays_single(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path | None]] = []

    def fake_manifest_validation(*, assets_root: Path) -> list[str]:
        calls.append(("manifest", assets_root))
        return []

    def fake_browser_validation() -> list[str]:
        calls.append(("browser", None))
        return []

    monkeypatch.setattr(VALIDATOR, "validate_spoken_asset_manifest", fake_manifest_validation)
    monkeypatch.setattr(VALIDATOR, "validate_browser_narration_pack", fake_browser_validation)

    assert VALIDATOR.validate_requested_assets(None) == []
    assert calls == [
        ("manifest", VALIDATOR.DEMO_ASSETS_ROOT),
        ("browser", None),
    ]

    calls.clear()
    custom_root = tmp_path / "custom-assets"
    assert VALIDATOR.validate_requested_assets(custom_root) == []
    assert calls == [("manifest", custom_root)]


def test_browser_narration_inventory_requires_all_eight_named_clips(
    copied_pack: tuple[Path, Path],
    stub_browser_media_tools: None,
) -> None:
    static_root, audio_root = copied_pack

    def remove_success(manifest: dict[str, object]) -> None:
        assets = manifest["assets"]
        assert isinstance(assets, list)
        manifest["assets"] = [entry for entry in assets if entry["path"] != "first_listen/success.mp3"]

    _rewrite_manifest(audio_root, remove_success)

    errors = VALIDATOR.validate_browser_narration_pack(assets_root=audio_root, static_root=static_root)

    assert any("browser narration inventory is missing: first_listen/success.mp3" in error for error in errors)


def test_browser_narration_hash_drift_fails(
    copied_pack: tuple[Path, Path],
    stub_browser_media_tools: None,
) -> None:
    static_root, audio_root = copied_pack
    (audio_root / "first_listen/welcome.mp3").write_bytes(b"tampered")

    errors = VALIDATOR.validate_browser_narration_pack(assets_root=audio_root, static_root=static_root)

    assert "first_listen/welcome.mp3 sha256 does not match" in errors


def test_browser_narration_rejects_stale_canonical_receipt(
    copied_pack: tuple[Path, Path],
    stub_browser_media_tools: None,
) -> None:
    static_root, audio_root = copied_pack

    def change_voice(manifest: dict[str, object]) -> None:
        receipt = manifest["canonical_render_receipt"]
        assert isinstance(receipt, dict)
        hosts = receipt["hosts"]
        assert isinstance(hosts, list)
        hosts[0]["voice_id"] = "stale-voice-id"

    _rewrite_manifest(audio_root, change_voice)

    errors = VALIDATOR.validate_browser_narration_pack(assets_root=audio_root, static_root=static_root)

    assert "canonical_render_receipt does not match the current Marco/Giulia radio.toml config" in errors


def test_browser_narration_rejects_fallback_render(
    copied_pack: tuple[Path, Path],
    stub_browser_media_tools: None,
) -> None:
    static_root, audio_root = copied_pack

    def mark_as_fallback(manifest: dict[str, object]) -> None:
        manifest["render_provider"] = "edge"
        receipt = manifest["canonical_render_receipt"]
        assert isinstance(receipt, dict)
        receipt["fallback"] = True

    _rewrite_manifest(audio_root, mark_as_fallback)

    errors = VALIDATOR.validate_browser_narration_pack(assets_root=audio_root, static_root=static_root)

    assert "browser narration render_provider must be canonical; fallback audio cannot ship" in errors
    assert "canonical_render_receipt does not match the current Marco/Giulia radio.toml config" in errors


@pytest.mark.requires_ffmpeg
def test_hash_approved_non_audio_still_fails_ffprobe(copied_pack: tuple[Path, Path]) -> None:
    static_root, audio_root = copied_pack
    relative_path = "first_listen/sound-check.mp3"
    payload = b"not an audio stream" * 200
    (audio_root / relative_path).write_bytes(payload)

    def approve_payload(manifest: dict[str, object]) -> None:
        assets = manifest["assets"]
        assert isinstance(assets, list)
        entry = next(item for item in assets if item["path"] == relative_path)
        entry["sha256"] = hashlib.sha256(payload).hexdigest()

    _rewrite_manifest(audio_root, approve_payload)

    errors = VALIDATOR.validate_browser_narration_pack(assets_root=audio_root, static_root=static_root)

    assert any(error.startswith(f"{relative_path} is not ffprobe-readable audio") for error in errors)


def test_browser_narration_enforces_media_format_and_duration(
    copied_pack: tuple[Path, Path],
    stub_browser_media_tools: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    static_root, audio_root = copied_pack
    real_probe = VALIDATOR._probe_audio

    def wrong_welcome_format(path: Path, *, ffprobe: str):
        media, error = real_probe(path, ffprobe=ffprobe)
        if path.name != "welcome.mp3" or media is None:
            return media, error
        stream = dict(media["stream"])
        stream.update(
            codec_name="aac",
            sample_rate="44100",
            channels=1,
            channel_layout="mono",
            bit_rate="128000",
        )
        format_data = dict(media["format"])
        format_data["duration"] = "2.000"
        return {"stream": stream, "format": format_data}, None

    monkeypatch.setattr(VALIDATOR, "_probe_audio", wrong_welcome_format)

    errors = VALIDATOR.validate_browser_narration_pack(assets_root=audio_root, static_root=static_root)

    assert "first_listen/welcome.mp3 codec must be mp3; got 'aac'" in errors
    assert "first_listen/welcome.mp3 sample rate must be 48000 Hz; got '44100'" in errors
    assert "first_listen/welcome.mp3 channel count must be 2; got 1" in errors
    assert "first_listen/welcome.mp3 channel layout must be stereo; got 'mono'" in errors
    assert "first_listen/welcome.mp3 audio bitrate must be 192000 bps; got '128000'" in errors
    assert any("first_listen/welcome.mp3 duration 2.000s is outside" in error for error in errors)
    assert any(
        "first_listen/welcome.mp3 duration_seconds 11.856s does not match ffprobe 2.000s" in error for error in errors
    )


def test_browser_narration_enforces_measured_loudness_and_true_peak(
    copied_pack: tuple[Path, Path],
    stub_browser_media_tools: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    static_root, audio_root = copied_pack
    real_measure = VALIDATOR._measure_loudness

    def out_of_bounds(path: Path, *, ffmpeg: str):
        if path.name == "privacy.mp3":
            return (-20.0, -0.5), None
        return real_measure(path, ffmpeg=ffmpeg)

    monkeypatch.setattr(VALIDATOR, "_measure_loudness", out_of_bounds)

    errors = VALIDATOR.validate_browser_narration_pack(assets_root=audio_root, static_root=static_root)

    assert any("first_listen/privacy.mp3 integrated loudness -20.0 LUFS is outside" in error for error in errors)
    assert "first_listen/privacy.mp3 true peak -0.5 dBTP exceeds -1.0 dBTP" in errors


def test_browser_narration_bundle_cannot_cross_size_ceiling(
    copied_pack: tuple[Path, Path],
    stub_browser_media_tools: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    static_root, audio_root = copied_pack
    monkeypatch.setattr(VALIDATOR, "BROWSER_GUIDE_MAX_BYTES", 1)

    errors = VALIDATOR.validate_browser_narration_pack(assets_root=audio_root, static_root=static_root)

    assert any("browser narration bundle is" in error and "maximum is 1 bytes" in error for error in errors)


def test_browser_narration_files_must_resolve_through_public_static_root(
    copied_pack: tuple[Path, Path],
    tmp_path: Path,
    stub_browser_media_tools: None,
) -> None:
    _static_root, audio_root = copied_pack

    errors = VALIDATOR.validate_browser_narration_pack(
        assets_root=audio_root,
        static_root=tmp_path / "wrong-static-root",
    )

    assert any("does not resolve to this file through /static/audio" in error for error in errors)


def test_browser_transcripts_require_both_hosts_and_listener_truth(
    copied_pack: tuple[Path, Path],
    stub_browser_media_tools: None,
) -> None:
    static_root, audio_root = copied_pack

    def break_transcripts(manifest: dict[str, object]) -> None:
        assets = manifest["assets"]
        assert isinstance(assets, list)
        welcome = next(item for item in assets if item["path"] == "first_listen/welcome.mp3")
        welcome["transcript"] = welcome["transcript"].replace("Marco:", "Host:")
        privacy = next(item for item in assets if item["path"] == "first_listen/privacy.mp3")
        privacy["transcript"] += " Someone just tuned in."

    _rewrite_manifest(audio_root, break_transcripts)

    errors = VALIDATOR.validate_browser_narration_pack(assets_root=audio_root, static_root=static_root)

    assert "first_listen/welcome.mp3 transcript must include Marco" in errors
    assert "first_listen/privacy.mp3 transcript contains listener arrival/return copy" in errors


@pytest.mark.asyncio
async def test_all_browser_narration_clips_are_public_audio_mpeg() -> None:
    from mammamiradio.web.streamer import router

    app = FastAPI()
    app.include_router(router)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        for relative_path in EXPECTED_CLIPS:
            response = await client.get(f"/static/audio/{relative_path}")
            assert response.status_code == 200, relative_path
            assert response.headers["content-type"].startswith("audio/mpeg"), relative_path
            assert response.content, relative_path

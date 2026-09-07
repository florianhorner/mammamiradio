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


@pytest.mark.requires_ffmpeg
def test_shipped_demo_banter_is_package_reachable_and_playable() -> None:
    assert VALIDATOR.validate_demo_spoken_assets() == []


def test_demo_banter_validator_enforces_media_loudness_and_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    banter = tmp_path / "banter"
    banter.mkdir()
    payload = b"reviewed demo audio" * 200
    clip = banter / "clip.mp3"
    clip.write_bytes(payload)
    (tmp_path / "spoken_assets.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assets": [
                    {
                        "path": "banter/clip.mp3",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "kind": "speech",
                        "language": "en",
                        "transcript": "Marco: Studio B keeps its own records.",
                        "mode": "normal",
                        "required_previous_starter_id": "",
                        "special": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(VALIDATOR.shutil, "which", lambda command: f"/test-bin/{command}")
    monkeypatch.setattr(
        VALIDATOR,
        "_probe_audio",
        lambda path, *, ffprobe: (
            {
                "stream": {
                    "codec_type": "audio",
                    "codec_name": "mp3",
                    "sample_rate": "44100",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "bit_rate": "192000",
                },
                "format": {"duration": "60.0"},
            },
            None,
        ),
    )
    monkeypatch.setattr(VALIDATOR, "_measure_loudness", lambda path, *, ffmpeg: ((-20.0, -0.5), None))
    monkeypatch.setattr(VALIDATOR, "DEMO_BANTER_MAX_BYTES", 1)

    errors = VALIDATOR.validate_demo_spoken_assets(assets_root=tmp_path, package_assets_root=tmp_path)

    assert any("sample_rate must be 48000" in error for error in errors)
    assert any("integrated loudness -20.0 LUFS is outside" in error for error in errors)
    assert any("true peak -0.5 dBTP exceeds -1.0 dBTP" in error for error in errors)
    assert any("demo banter bundle is" in error and "maximum is 1 bytes" in error for error in errors)


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
        clip=None,
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
    assert args.clip is None  # The established full-pack default is unchanged.
    assert not hasattr(args, "provider")

    monkeypatch.setattr(sys, "argv", ["generate-first-listen-guide.py", "--clip", "welcome"])
    assert GENERATOR._arguments().clip == "welcome"

    monkeypatch.setattr(sys, "argv", ["generate-first-listen-guide.py", "--station-opening"])
    assert GENERATOR._arguments().output_root == GENERATOR.STATION_OUTPUT_ROOT
    monkeypatch.setattr(sys, "argv", ["generate-first-listen-guide.py", "--station-opening", "--clip", "welcome"])
    with pytest.raises(SystemExit):
        GENERATOR._arguments()

    monkeypatch.setattr(sys, "argv", ["generate-first-listen-guide.py", "--provider", "edge"])
    with pytest.raises(SystemExit) as exc_info:
        GENERATOR._arguments()
    assert exc_info.value.code == 2


@pytest.fixture
def generator_media_tools(stub_browser_media_tools, monkeypatch):
    monkeypatch.setattr(GENERATOR, "_load_pack_validator", lambda: VALIDATOR.validate_browser_narration_pack)


@pytest.mark.parametrize("selected", ["welcome", None])
def test_generator_stages_selected_clips_and_preserves_retained_pack(
    copied_pack, monkeypatch, selected, generator_media_tools
) -> None:
    _static_root, copied_pack = copied_pack
    before = {path.name: path.read_bytes() for path in (copied_pack / "first_listen").glob("*.mp3")}
    original = json.loads((copied_pack / "spoken_assets.json").read_text())
    calls = []

    async def render(clip, _hosts, _work_dir, destination, **_kwargs):
        calls.append(clip.clip_id)
        destination.write_bytes(b"replacement " + clip.clip_id.encode())
        return {
            **next(entry for entry in original["assets"] if entry["path"].endswith(f"/{clip.clip_id}.mp3")),
            "sha256": GENERATOR._sha256(destination),
            "transcript": clip.transcript,
        }

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-no-provider-calls")
    monkeypatch.setattr(GENERATOR, "_render_clip", render)
    asyncio.run(
        GENERATOR._run(argparse.Namespace(env_file=copied_pack / "absent.env", output_root=copied_pack, clip=selected))
    )
    after = json.loads((copied_pack / "spoken_assets.json").read_text())
    assert calls == (["welcome"] if selected else [clip.clip_id for clip in GENERATOR.GUIDE_CLIPS])
    assert len(after["assets"]) == 7
    for old, new in zip(original["assets"], after["assets"], strict=True):
        if selected and not old["path"].endswith("/welcome.mp3"):
            assert old == new
            assert (copied_pack / old["path"]).read_bytes() == before[Path(old["path"]).name]
        else:
            assert old["sha256"] != new["sha256"]


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "unlisted",
        "inventory",
        "hash",
        "receipt",
        "provider",
        "malformed",
        "duration",
        "speakers",
        "transcript",
    ],
)
def test_welcome_render_refuses_invalid_retained_pack_before_synthesis(
    copied_pack, monkeypatch, damage, generator_media_tools
) -> None:
    _static_root, copied_pack = copied_pack
    if damage == "missing":
        (copied_pack / "first_listen/privacy.mp3").unlink()
    elif damage == "unlisted":
        (copied_pack / "first_listen/extra.mp3").write_bytes(b"unexpected")
    elif damage == "hash":
        (copied_pack / "first_listen/privacy.mp3").write_bytes(b"tampered")
    elif damage == "malformed":
        (copied_pack / "spoken_assets.json").write_text("[]")
    else:

        def mutate(manifest):
            if damage == "inventory":
                manifest["assets"].pop()
                (copied_pack / "first_listen/success.mp3").unlink()
            elif damage == "receipt":
                manifest["canonical_render_receipt"]["hosts"][0]["voice_id"] = "wrong-voice"
            elif damage in {"duration", "speakers", "transcript"}:
                entry = next(item for item in manifest["assets"] if item["path"] == "first_listen/privacy.mp3")
                entry[{"duration": "duration_seconds", "speakers": "speakers", "transcript": "transcript"}[damage]] = (
                    "Marco: Only one host." if damage == "transcript" else None
                )
            else:
                manifest["render_provider"] = "edge"

        _rewrite_manifest(copied_pack, mutate)

    async def reject_render(*_args, **_kwargs):
        pytest.fail("invalid retained pack reached paid synthesis")

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-no-provider-calls")
    monkeypatch.setattr(GENERATOR, "_render_clip", reject_render)
    before = {p.relative_to(copied_pack): p.read_bytes() for p in copied_pack.rglob("*") if p.is_file()}
    with pytest.raises(RuntimeError):
        asyncio.run(
            GENERATOR._run(
                argparse.Namespace(env_file=copied_pack / "absent.env", output_root=copied_pack, clip="welcome")
            )
        )
    assert before == {p.relative_to(copied_pack): p.read_bytes() for p in copied_pack.rglob("*") if p.is_file()}


@pytest.mark.parametrize("failure", ["synthesis", "validation", "media"])
@pytest.mark.parametrize("selected", ["welcome", None])
def test_failed_render_or_staged_validation_leaves_shipped_pack_untouched(
    copied_pack, monkeypatch, failure, selected, generator_media_tools
) -> None:
    _static_root, copied_pack = copied_pack
    original = json.loads((copied_pack / "spoken_assets.json").read_text())
    before = {p.relative_to(copied_pack): p.read_bytes() for p in copied_pack.rglob("*") if p.is_file()}

    async def render(clip, _hosts, _work_dir, destination, **_kwargs):
        destination.write_bytes(b"unfinished replacement")
        if failure == "synthesis":
            raise RuntimeError("provider render failed")
        entry = next(entry for entry in original["assets"] if entry["path"].endswith(f"/{clip.clip_id}.mp3"))
        if failure == "media":
            monkeypatch.setattr(VALIDATOR, "_probe_audio", lambda *_args, **_kwargs: (None, "not playable audio"))
            return {**entry, "sha256": GENERATOR._sha256(destination)}
        return entry

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-no-provider-calls")
    monkeypatch.setattr(GENERATOR, "_render_clip", render)
    with pytest.raises(RuntimeError, match=r"provider render failed|sha256 does not match|not playable audio"):
        asyncio.run(
            GENERATOR._run(
                argparse.Namespace(env_file=copied_pack / "absent.env", output_root=copied_pack, clip=selected)
            )
        )
    assert before == {p.relative_to(copied_pack): p.read_bytes() for p in copied_pack.rglob("*") if p.is_file()}


@pytest.mark.requires_ffmpeg
def test_generator_rejects_hash_bound_non_audio_before_render(copied_pack, monkeypatch) -> None:
    _static, root = copied_pack
    path = root / "first_listen/privacy.mp3"
    path.write_bytes(b"not an MP3 even with a matching hash")
    _rewrite_manifest(
        root,
        lambda manifest: next(
            entry for entry in manifest["assets"] if entry["path"] == "first_listen/privacy.mp3"
        ).update(sha256=GENERATOR._sha256(path)),
    )
    manifest = json.loads((root / "spoken_assets.json").read_text())
    with pytest.raises(RuntimeError):
        GENERATOR._validated_pack(root, manifest["canonical_render_receipt"])


def test_staged_render_validation_keeps_release_ui_binding_checks(copied_pack, generator_media_tools) -> None:
    static, root = copied_pack
    _rewrite_manifest(
        root,
        lambda manifest: manifest["assets"][0].update(transcript="Marco: Updated welcome. Giulia: Three small steps."),
    )
    assert VALIDATOR.validate_browser_narration_pack(assets_root=root, staged_render=True) == []
    assert any(
        "transcript" in error
        for error in VALIDATOR.validate_browser_narration_pack(assets_root=root, static_root=static)
    )


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

    # Derive both mutations from the shipped manifest. Hardcoded literals here
    # silently stop matching the moment the guide pack is regenerated, and a
    # substitution that no-ops turns this drift guard into a test that passes
    # against an unmutated template — so each one must be asserted to apply.
    welcome = next(entry for entry in manifest["assets"] if entry["path"] == "first_listen/welcome.mp3")
    real_version = f"version:'{welcome['sha256'][:12]}'"
    # Marco's spoken line appears verbatim between the <strong> tags in the page.
    marco_line = welcome["transcript"].split("Marco: ", 1)[1].split(" Giulia:", 1)[0]

    assert source.count(real_version) == 1, "welcome version literal is not uniquely present in the template"
    assert source.count(marco_line) == 1, "welcome transcript line is not uniquely present in the template"

    source = source.replace(real_version, "version:'000000000000'", 1)
    source = source.replace(marco_line, "This copy drifted.", 1)
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
        "Preview 16-second welcome</button>"
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

    calls.clear()

    def fake_demo_validation(*, assets_root: Path) -> list[str]:
        calls.append(("demo", assets_root))
        return []

    monkeypatch.setattr(VALIDATOR, "validate_demo_spoken_assets", fake_demo_validation)
    assert VALIDATOR.validate_requested_assets(VALIDATOR.DEMO_ASSETS_ROOT) == []
    assert calls == [("demo", VALIDATOR.DEMO_ASSETS_ROOT)]


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
    manifest = json.loads((audio_root / "spoken_assets.json").read_text(encoding="utf-8"))
    declared = next(entry for entry in manifest["assets"] if entry["path"] == "first_listen/welcome.mp3")
    assert any(
        f"first_listen/welcome.mp3 duration_seconds {declared['duration_seconds']:.3f}s "
        "does not match ffprobe 2.000s" in error
        for error in errors
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


@pytest.mark.parametrize("default_root", [False, True])
@pytest.mark.parametrize("failure", [None, "inventory", "hash", "voice", "render", "validation"])
def test_station_render_preserves_both_packs_and_fails_before_publication(
    tmp_path, monkeypatch, generator_media_tools, failure, default_root
):
    root = tmp_path / "demo"
    shutil.copytree(GENERATOR.STATION_OUTPUT_ROOT, root)
    if default_root:
        monkeypatch.setattr(GENERATOR, "STATION_OUTPUT_ROOT", root)
    browser_before = {p: p.read_bytes() for p in SHIPPED_AUDIO_ROOT.rglob("*.mp3")}
    original = json.loads((root / "spoken_assets.json").read_text())
    if failure == "inventory":
        removed = original["assets"].pop(0)
        (root / removed["path"]).unlink()
    elif failure == "hash":
        (root / original["assets"][0]["path"]).write_bytes(b"tampered")
    elif failure == "voice":
        opening = next(entry for entry in original["assets"] if entry["path"] == VALIDATOR.ADMIN_STATION_OPENING_PATH)
        opening["canonical_render_receipt"]["hosts"][0]["voice_id"] = "incompatible-voice"
    (root / "spoken_assets.json").write_text(json.dumps(original))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    calls = []

    async def render(clip, _hosts, _work, destination, **_kwargs):
        calls.append(clip.clip_id)
        assert clip is GENERATOR.STATION_OPENING_CLIP
        destination.write_bytes(b"replacement station recording")
        if failure == "render":
            raise RuntimeError("synthesis failed")
        return {
            "path": f"first_listen/{clip.clip_id}.mp3",
            "sha256": GENERATOR._sha256(destination),
            "kind": "speech",
            "language": "en",
            "transcript": clip.transcript,
            "duration_seconds": 17.0,
            "speakers": [line.host for line in clip.lines],
        }

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-no-provider-calls")
    monkeypatch.setattr(GENERATOR, "_render_clip", render)
    monkeypatch.setattr(
        GENERATOR.runpy,
        "run_path",
        lambda _path: {
            "DEMO_SPOKEN_PATHS": VALIDATOR.DEMO_SPOKEN_PATHS,
            "validate_spoken_asset_manifest": VALIDATOR.validate_spoken_asset_manifest,
            "validate_demo_spoken_assets": lambda **kwargs: (
                ["invalid staged media"]
                if failure == "validation"
                else VALIDATOR.validate_spoken_asset_manifest(assets_root=kwargs["assets_root"])
            ),
        },
    )
    args = argparse.Namespace(env_file=root / "absent.env", output_root=root, clip=None, station_opening=True)
    if failure:
        with pytest.raises(RuntimeError):
            asyncio.run(GENERATOR._run(args))
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
        assert len(calls) == (0 if failure in {"inventory", "hash", "voice"} else 1)
    else:
        asyncio.run(GENERATOR._run(args))
        assert calls == ["first_listen_admin_show"]
        after = json.loads((root / "spoken_assets.json").read_text())
        for entry in original["assets"]:
            if not entry["path"].endswith("first_listen_admin_show.mp3"):
                assert entry in after["assets"]
                assert (root / entry["path"]).read_bytes() == before[Path(entry["path"])]
    assert all(p.read_bytes() == payload for p, payload in browser_before.items())


@pytest.mark.requires_ffmpeg
def test_station_opening_media_and_transcript_match_approved_script():
    root = GENERATOR.STATION_OUTPUT_ROOT
    manifest = json.loads((root / "spoken_assets.json").read_text())
    entry = next(entry for entry in manifest["assets"] if entry["path"] == VALIDATOR.ADMIN_STATION_OPENING_PATH)
    assert entry["transcript"] == GENERATOR.STATION_OPENING_CLIP.transcript
    assert entry["canonical_render_receipt"] == VALIDATOR._canonical_render_receipt()
    assert VALIDATOR.validate_demo_spoken_assets() == []


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("missing", None, "inventory must contain"),
        ("language", "it", "English Marco/Giulia"),
        ("speakers", ["Giulia"], "English Marco/Giulia"),
        ("canonical_render_receipt", {}, "canonical_render_receipt does not match"),
        ("duration_seconds", 2, "declared duration does not match"),
        ("measured", 14.9, "outside 15.0-20.0s"),
        ("measured", 20.1, "outside 15.0-20.0s"),
    ],
)
def test_station_opening_validator_rejects_invalid_retained_media(tmp_path, monkeypatch, field, value, error):
    root = tmp_path / "demo"
    shutil.copytree(GENERATOR.STATION_OUTPUT_ROOT, root)
    manifest = json.loads((root / "spoken_assets.json").read_text())
    entry = next(item for item in manifest["assets"] if item["path"] == VALIDATOR.ADMIN_STATION_OPENING_PATH)
    if field == "missing":
        manifest["assets"].remove(entry)
    elif field != "measured":
        entry[field] = value
    (root / "spoken_assets.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(VALIDATOR, "_is_demo_assets_root", lambda _root: True)
    monkeypatch.setattr(VALIDATOR.shutil, "which", lambda command: f"/test-bin/{command}")
    monkeypatch.setattr(VALIDATOR, "_measure_loudness", lambda path, **kwargs: ((-16.0, -2.0), None))

    def probe(path, **kwargs):
        opening = path.name == "first_listen_admin_show.mp3"
        duration = value if opening and field == "measured" else 15.096 if opening else 30.0
        stream = {
            "codec_type": "audio",
            "codec_name": "mp3",
            "sample_rate": 48000,
            "channels": 2,
            "channel_layout": "stereo",
            "bit_rate": 192000,
        }
        return {"stream": stream, "format": {"duration": duration}}, None

    monkeypatch.setattr(VALIDATOR, "_probe_audio", probe)
    errors = VALIDATOR.validate_demo_spoken_assets(assets_root=root, package_assets_root=root)
    assert any(error in message for message in errors), errors

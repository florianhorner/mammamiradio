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
        side = path.parent.name == "voice_examples"
        manifest = json.loads(((path.parent if side else path.parents[1]) / "spoken_assets.json").read_text())
        relative_path = path.name if side else f"first_listen/{path.name}"
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
    monkeypatch.setattr(sys, "argv", ["generate-first-listen-guide.py", "--clip", "privacy"])
    assert GENERATOR._arguments().clip == "privacy"
    monkeypatch.setattr(sys, "argv", ["generate-first-listen-guide.py", "--clip", "ai"])
    assert GENERATOR._arguments().clip == "ai"

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
    monkeypatch.setattr(GENERATOR.runpy, "run_path", lambda _path: vars(VALIDATOR))


def _free_manifest():
    return json.loads((SHIPPED_AUDIO_ROOT / "voice_examples/spoken_assets.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("host_name", ["Marco", "Giulia"])
async def test_free_voice_render_uses_exact_voice_and_blocks_transport_retry(tmp_path, monkeypatch, retry, host_name):
    import aiohttp
    import edge_tts

    from mammamiradio.audio import normalizer
    from mammamiradio.core.config import load_config

    host = next(host for host in load_config(str(ROOT / "radio.toml")).hosts if host.name == host_name)
    requests = []

    async def connect(*args, **kwargs):
        requests.append(1)

    class Voice:
        def __init__(self, text, voice, *, rate, pitch, connector):
            assert (text, voice, rate, pitch) == ("approved line", host.edge_fallback_voice, "+0%", "+0Hz")
            self.connector = connector

        async def save(self, path):
            await self.connector.connect()
            if retry:
                await self.connector.connect()
            Path(path).write_bytes(b"raw provider take")

    monkeypatch.setattr(aiohttp.TCPConnector, "connect", connect)
    monkeypatch.setattr(edge_tts, "Communicate", Voice)
    monkeypatch.setattr(normalizer, "normalize", lambda raw, out, **kwargs: out.write_bytes(raw.read_bytes()))
    out = tmp_path / "voice.mp3"
    if retry:
        with pytest.raises(RuntimeError, match="automatic retry is disabled"):
            await GENERATOR._render_free_line(host, "approved line", out)
        assert not out.exists()
    else:
        assert await GENERATOR._render_free_line(host, "approved line", out) == out
        assert out.read_bytes() == out.with_suffix(".edge-raw.mp3").read_bytes()
    assert len(requests) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "audio",
        "extra",
        "symlink",
        "manifest-link",
        "directory-link",
        "assets-null",
        "assets-scalar",
        "hash",
        "path",
        "duplicate",
        "receipt",
        "transcript",
        "media",
        "budget",
    ],
)
def test_free_example_damage_blocks_release_and_generation_before_provider(
    copied_pack, monkeypatch, generator_media_tools, damage
):
    static, root = copied_pack
    side = root / "voice_examples"
    manifest = side / "spoken_assets.json"
    data = json.loads(manifest.read_text())
    asset = side / "free-voices.mp3"
    if damage == "missing":
        manifest.unlink()
    elif damage == "audio":
        asset.unlink()
    elif damage == "extra":
        (side / "extra.mp3").write_bytes(b"unlisted")
    elif damage in {"symlink", "manifest-link"}:
        target = asset if damage == "symlink" else manifest
        outside = root.parent / target.name
        target.rename(outside)
        target.symlink_to(outside)
    elif damage == "directory-link":
        shutil.rmtree(side)
        side.symlink_to(root.parent / "missing-directory", target_is_directory=True)
    elif damage in {"assets-null", "assets-scalar"}:
        data["assets"] = None if damage == "assets-null" else 3
        manifest.write_text(json.dumps(data))
    elif damage == "media":
        monkeypatch.setattr(VALIDATOR, "_probe_audio", lambda *a, **kw: (None, "not playable audio"))
    elif damage == "budget":
        monkeypatch.setattr(
            VALIDATOR, "BROWSER_GUIDE_MAX_BYTES", sum(p.stat().st_size for p in (root / "first_listen").glob("*.mp3"))
        )
    else:
        if damage == "receipt":
            data["render_receipt"]["hosts"][0]["voice_id"] = "wrong voice"
        elif damage == "duplicate":
            data["assets"].append(data["assets"][0])
        else:
            data["assets"][0][{"hash": "sha256", "path": "path", "transcript": "transcript"}[damage]] = "changed"
        manifest.write_text(json.dumps(data))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert VALIDATOR.validate_browser_narration_pack(assets_root=root, static_root=static)

    async def forbidden(*args, **kwargs):
        pytest.fail("provider called before retained pack rejection")

    monkeypatch.setattr(GENERATOR, "_render_clip", forbidden)
    for free, selected in [(True, None), (False, "welcome"), (False, None)]:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "unused-test-key")
        with pytest.raises(RuntimeError):
            asyncio.run(
                GENERATOR._run(
                    argparse.Namespace(
                        env_file=root / "absent.env", output_root=root, clip=selected, free_voice_example=free
                    )
                )
            )
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", [None, "synthesis", "validation", "publish-audio", "publish-manifest"])
def test_free_example_stages_without_paid_key_and_rolls_back(
    copied_pack, monkeypatch, generator_media_tools, existing, failure
):
    _static, root = copied_pack
    original = _free_manifest()
    if not existing:
        shutil.rmtree(root / "voice_examples")
        assert VALIDATOR.validate_browser_narration_pack(
            assets_root=root
        )  # Required at release, even on first creation.
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    calls = []

    async def render(clip, _hosts, _work, destination, **kwargs):
        assert kwargs["free_voices"] is True and clip is GENERATOR.FREE_VOICE_CLIP
        calls.append(clip.clip_id)
        destination.write_bytes(b"new approved free sample")
        if failure == "synthesis":
            raise RuntimeError("synthesis failed")
        return {
            **original["assets"][0],
            "sha256": GENERATOR._sha256(destination),
            "transcript": "wrong" if failure == "validation" else clip.transcript,
        }

    publish = GENERATOR._publish_staged_file

    def guarded_publish(source, destination):
        if failure == "publish-audio" or (failure == "publish-manifest" and destination.name == "spoken_assets.json"):
            raise OSError("simulated publication failure")
        publish(source, destination)

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(GENERATOR, "_load_environment", lambda path: None)
    monkeypatch.setattr(GENERATOR, "_render_clip", render)
    monkeypatch.setattr(GENERATOR, "_publish_staged_file", guarded_publish)
    args = argparse.Namespace(env_file=root / "absent.env", output_root=root, clip=None, free_voice_example=True)
    if failure:
        with pytest.raises((RuntimeError, OSError)):
            asyncio.run(GENERATOR._run(args))
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
        if not existing:
            assert not (root / "voice_examples").exists(), "failed creation stranded a partial pack"
    else:
        asyncio.run(GENERATOR._run(args))
        assert VALIDATOR.validate_free_voice_example(assets_root=root, staged_render=True) == []
        assert all(
            (root / p).read_bytes() == content for p, content in before.items() if p.parts[0] != "voice_examples"
        )
    assert calls == ["free-voices"]


def test_free_example_bindings_and_cli_are_exact(monkeypatch):
    entry = _free_manifest()["assets"][0]
    assert entry["transcript"] == GENERATOR.FREE_VOICE_CLIP.transcript == VALIDATOR.FREE_VOICE_TRANSCRIPT
    source = (ROOT / "mammamiradio/web/templates/admin.html").read_text()
    block = source.split('data-guide="free-voices"', 1)[1].split("</details>", 1)[0]
    assert f"{round(entry['duration_seconds'])} seconds on this device" in block
    for field in ["sha256", "transcript"]:
        damaged = _free_manifest()
        damaged["assets"][0][field] = "wrong"
        assert VALIDATOR._validate_admin_guide_metadata(
            json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text()), voice_manifest=damaged
        )
    monkeypatch.setattr(sys, "argv", ["generate-first-listen-guide.py", "--free-voice-example"])
    assert GENERATOR._arguments().free_voice_example
    for selection in [["--clip", "welcome"], ["--station-opening"]]:
        monkeypatch.setattr(sys, "argv", ["generate-first-listen-guide.py", "--free-voice-example", *selection])
        with pytest.raises(SystemExit):
            GENERATOR._arguments()


@pytest.mark.parametrize("publish_failure", [False, True])
@pytest.mark.parametrize("selected", ["welcome", None])
def test_generator_stages_selected_clips_and_preserves_retained_pack(
    copied_pack, monkeypatch, selected, publish_failure, generator_media_tools
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
    if publish_failure:
        publish = GENERATOR._publish_staged_file

        def fail_manifest(source, destination):
            if destination.name == "spoken_assets.json":
                raise OSError("manifest publication failed")
            publish(source, destination)

        monkeypatch.setattr(GENERATOR, "_publish_staged_file", fail_manifest)
        with pytest.raises(OSError, match="manifest publication failed"):
            asyncio.run(
                GENERATOR._run(
                    argparse.Namespace(env_file=copied_pack / "absent.env", output_root=copied_pack, clip=selected)
                )
            )
        assert json.loads((copied_pack / "spoken_assets.json").read_text()) == original
        assert {path.name: path.read_bytes() for path in (copied_pack / "first_listen").glob("*.mp3")} == before
        return
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


@pytest.mark.parametrize("existing_mode", [None, 0o600, 0o640, 0o644])
def test_generator_publish_handles_cross_filesystem_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_mode: int | None,
) -> None:
    staged = tmp_path / "os-temp" / "welcome.mp3"
    destination = tmp_path / "external-output" / "welcome.mp3"
    staged.parent.mkdir()
    destination.parent.mkdir()
    staged.write_bytes(b"rendered audio")
    staged.chmod(0o600)
    if existing_mode is not None:
        destination.write_bytes(b"old audio")
        destination.chmod(existing_mode)
    real_rename = os.rename

    def cross_device_rename(source, target):
        if Path(source) == staged:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_rename(source, target)

    monkeypatch.setattr(os, "rename", cross_device_rename)

    GENERATOR._publish_staged_file(staged, destination)

    assert destination.read_bytes() == b"rendered audio"
    assert destination.stat().st_mode & 0o777 == (0o644 if existing_mode is None else existing_mode)
    assert not staged.exists()


def test_admin_guide_metadata_matches_shipped_manifest() -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))

    assert VALIDATOR._validate_admin_guide_metadata(manifest, voice_manifest=_free_manifest()) == []


def test_welcome_transcript_duration_and_cache_binding_are_synchronized() -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    template = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    welcome = next(entry for entry in manifest["assets"] if entry["path"] == "first_listen/welcome.mp3")
    assert welcome["transcript"] == GENERATOR.GUIDE_CLIPS[0].transcript
    assert "Three small steps" in welcome["transcript"]
    assert "I run the desk" in welcome["transcript"]
    assert f"version:'{welcome['sha256'][:12]}'" in template
    assert f"{round(welcome['duration_seconds'])} seconds on this device" in template


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

    errors = VALIDATOR._validate_admin_guide_metadata(
        manifest, voice_manifest=_free_manifest(), admin_template_path=template_path
    )

    assert any("admin guide welcome version" in error and "manifest sha256 prefix" in error for error in errors)
    assert "admin guide welcome transcript does not match spoken_assets.json" in errors


def test_admin_guide_metadata_rejects_container_inventory_drift(tmp_path: Path) -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    source = source.replace('class="guide-audio" data-guide="welcome"', 'class="guide-audio" data-guide="extra"', 1)
    template_path = tmp_path / "admin.html"
    template_path.write_text(source, encoding="utf-8")

    errors = VALIDATOR._validate_admin_guide_metadata(
        manifest, voice_manifest=_free_manifest(), admin_template_path=template_path
    )

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

    errors = VALIDATOR._validate_admin_guide_metadata(
        manifest, voice_manifest=_free_manifest(), admin_template_path=template_path
    )

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

    errors = VALIDATOR._validate_admin_guide_metadata(
        manifest, voice_manifest=_free_manifest(), admin_template_path=template_path
    )

    assert "admin guide welcome play button has unrecognized data-guide-key 'unknown'" in errors


def test_admin_guide_metadata_requires_exactly_one_play_button_per_container(tmp_path: Path) -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    welcome_button = (
        '<button type="button" class="guide-audio-play" data-guide-key="welcome" '
        'aria-describedby="guideWelcomeNote" onclick="toggleFirstListenGuide(\'welcome\',this)">'
        "Take your seat</button>"
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

    errors = VALIDATOR._validate_admin_guide_metadata(
        manifest, voice_manifest=_free_manifest(), admin_template_path=template_path
    )

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


HOME_MOMENTS = ("quiet.mp3", "laundry.mp3", "arrival.mp3", "coffee.mp3")
HOME_MOMENT_ROOT = SHIPPED_AUDIO_ROOT / "home_moments"
EXPLAINER_AUDIO_ROOT = ROOT / "docs" / "explainer" / "public" / "audio"


def test_home_moments_copy_explainer_bytes_outside_the_guide_pack() -> None:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    declared = {entry["path"] for entry in manifest["assets"]}
    for name in HOME_MOMENTS:
        shipped = HOME_MOMENT_ROOT / name
        source = EXPLAINER_AUDIO_ROOT / name
        assert shipped.is_file(), name
        assert not shipped.is_symlink(), name
        assert shipped.read_bytes() == source.read_bytes(), name
        assert f"home_moments/{name}" not in declared
        assert f"first_listen/{name}" not in declared


def test_home_moment_pack_is_bound_to_the_explainer_source() -> None:
    """The explainer manifest is the single source of truth for these clips.

    Three copies of the same audio exist (explainer page, shipped pack, admin
    cache-bust tokens). Only a binding makes that safe: regenerate a clip and the
    validator fails rather than the pack silently drifting behind a stale URL.
    """

    manifest = json.loads((HOME_MOMENT_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))
    segments = json.loads((EXPLAINER_AUDIO_ROOT / "segments.manifest.json").read_text(encoding="utf-8"))["segments"]
    admin = (ROOT / "mammamiradio" / "web" / "templates" / "admin.html").read_text(encoding="utf-8")

    assert manifest["bundle"] == "first-listen-home-moments"
    reachabilities = {entry["path"]: entry["reachability"] for entry in manifest["assets"]}
    # Without a day-one entry Step 3 demonstrates only capability a fresh install
    # cannot reach. validate-spoken-assets.py refuses that; this pins the data.
    assert "day-one" in reachabilities.values()
    assert reachabilities["quiet.mp3"] == "day-one"

    for entry in manifest["assets"]:
        key = entry["path"].removesuffix(".mp3")
        digest = hashlib.sha256((HOME_MOMENT_ROOT / entry["path"]).read_bytes()).hexdigest()
        assert entry["sha256"] == digest, key
        assert segments[key]["sha256"] == digest, key
        assert f"{key}:{{file:'{entry['path']}',version:'{digest[:12]}'}}" in admin, key


def _rewrite_home_moments(audio_root: Path, mutate) -> None:
    manifest_path = audio_root / "home_moments" / "spoken_assets.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _home_moment_errors(copied_pack) -> list[str]:
    static_root, audio_root = copied_pack
    return VALIDATOR.validate_home_moments(assets_root=audio_root, static_root=static_root)


def test_home_moments_accept_the_shipped_pack(copied_pack) -> None:
    assert _home_moment_errors(copied_pack) == []


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        (lambda m: m.__setitem__("bundle", "wrong-bundle"), "schema or bundle is invalid"),
        (lambda m: m.__setitem__("assets", []), "declares no assets"),
        (lambda m: m["assets"].__setitem__(0, "not-a-dict"), "malformed entry"),
        (lambda m: m["assets"][0].__setitem__("path", "nested/quiet.mp3"), "must be a bare .mp3 name"),
        (lambda m: m["assets"][0].__setitem__("sha256", "0" * 64), "sha256 does not match"),
        (lambda m: m["assets"][0].__setitem__("reachability", "someday"), "reachability"),
        (lambda m: m["assets"][0].__setitem__("duration_seconds", 3.0), "is outside"),
        (lambda m: m["assets"][0].__setitem__("transcript", "   "), "transcript is missing"),
        (lambda m: m["assets"][0].pop("duration_seconds"), "duration_seconds is missing"),
    ],
)
def test_home_moment_manifest_drift_is_rejected(copied_pack, mutation, needle) -> None:
    """Every failure branch must actually fire.

    `scripts/` is outside the coverage ratchet (`[tool.coverage.run] source =
    ["mammamiradio"]`), so nothing else would notice a guard that cannot fail.
    """

    _, audio_root = copied_pack
    _rewrite_home_moments(audio_root, mutation)
    errors = _home_moment_errors(copied_pack)
    assert any(needle in error for error in errors), errors


def test_home_moments_reject_a_pack_with_nothing_reachable_today(copied_pack) -> None:
    """The refusal docs/explainer/scripts/build.mjs already makes, for Step 3."""

    _, audio_root = copied_pack

    def demote_all(manifest):
        for entry in manifest["assets"]:
            entry["reachability"] = "home-grant"

    _rewrite_home_moments(audio_root, demote_all)
    errors = _home_moment_errors(copied_pack)
    assert any("day-one" in error and "gated capability" in error for error in errors), errors


def test_home_moments_reject_unlisted_audio_and_missing_files(copied_pack) -> None:
    _, audio_root = copied_pack
    room = audio_root / "home_moments"
    shutil.copy(room / "quiet.mp3", room / "stray.mp3")
    assert any("unlisted audio" in error for error in _home_moment_errors(copied_pack))
    (room / "stray.mp3").unlink()
    (room / "quiet.mp3").unlink()
    # Assert the missing-file branch specifically: the generic inventory error
    # fires here too, so matching that would keep this green if the branch went.
    errors = _home_moment_errors(copied_pack)
    assert any("quiet.mp3 is missing or unreadable" in error for error in errors), errors


def test_home_moments_reject_bytes_that_are_not_the_explainer_segment(copied_pack) -> None:
    """The explainer pack is the single source of truth; drift either way fails."""

    _, audio_root = copied_pack
    room = audio_root / "home_moments"
    shutil.copy(room / "laundry.mp3", room / "quiet.mp3")
    errors = _home_moment_errors(copied_pack)
    assert any("is not the explainer segment it claims to copy" in error for error in errors), errors


def test_home_moments_reject_a_symlinked_clip(copied_pack) -> None:
    _, audio_root = copied_pack
    room = audio_root / "home_moments"
    (room / "quiet.mp3").unlink()
    (room / "quiet.mp3").symlink_to(room / "laundry.mp3")
    errors = _home_moment_errors(copied_pack)
    assert any("symlink" in error for error in errors), errors


def test_home_moments_reject_a_bundle_over_budget(copied_pack, monkeypatch) -> None:
    """Live headroom is thin: the pack is ~1.6 MiB against a 2 MiB cap."""

    monkeypatch.setattr(VALIDATOR, "HOME_MOMENT_MAX_BYTES", 1024)
    errors = _home_moment_errors(copied_pack)
    assert any("home moment bundle is" in error for error in errors), errors


def _admin_home_moment_errors(source: str) -> list[str]:
    manifest = json.loads((SHIPPED_AUDIO_ROOT / "home_moments" / "spoken_assets.json").read_text(encoding="utf-8"))
    errors, _ = VALIDATOR._validate_admin_home_moment_metadata(source, manifest)
    return errors


def test_admin_home_moment_metadata_accepts_the_shipped_template() -> None:
    assert _admin_home_moment_errors(VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")) == []


def test_admin_home_moment_metadata_rejects_a_stale_cache_bust_token() -> None:
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "quiet:{file:'quiet.mp3',version:'02fc7d83734a'}",
        "quiet:{file:'quiet.mp3',version:'deadbeef0000'}",
    )
    errors = _admin_home_moment_errors(source)
    assert any("does not match manifest sha256 prefix" in error for error in errors), errors


def test_admin_home_moment_metadata_rejects_a_chip_on_a_gated_scene() -> None:
    """Presence alone is not proof.

    An earlier version of this guard asked only whether *a* day-one scene wore
    the chip. A gated scene wearing one tells a fresh install the laundry works
    today, which narrow ambient context can never deliver.
    """

    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "<h5>The laundry finished. Nobody noticed.</h5>",
        '<h5>The laundry finished. Nobody noticed. <em class="day-one-chip">day one</em></h5>',
    )
    errors = _admin_home_moment_errors(source)
    assert any("carries a day-one-chip" in error for error in errors), errors


def test_admin_home_moment_metadata_rejects_reachability_swapped_against_the_manifest() -> None:
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    source = source.replace(
        'data-explainer-scenario="quiet" data-reachability="day-one"',
        'data-explainer-scenario="quiet" data-reachability="home-grant"',
    ).replace(
        'data-explainer-scenario="laundry" data-reachability="home-grant"',
        'data-explainer-scenario="laundry" data-reachability="day-one"',
    )
    errors = _admin_home_moment_errors(source)
    assert any("scene quiet declares data-reachability 'home-grant'" in e for e in errors), errors
    assert any("scene laundry declares data-reachability 'day-one'" in e for e in errors), errors


def test_admin_home_moment_metadata_rejects_a_chip_outside_the_heading() -> None:
    """The chip is styled by class alone, so it renders anywhere in the scene.

    An h5-bounded check passed while a reader still saw "day one" on a gated
    moment; this pins the subtree walk that replaced it.
    """

    template = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    for mutation in (
        (
            "<h5>The laundry finished. Nobody noticed.</h5>",
            '<h5>The laundry finished. Nobody noticed.</h5><em class="day-one-chip">day one</em>',
        ),
        (
            '<p class="scene-caption">Washing machine · finished</p>',
            '<p class="scene-caption">Washing machine · finished <em class="day-one-chip">day one</em></p>',
        ),
    ):
        errors = _admin_home_moment_errors(template.replace(*mutation))
        assert any("laundry is 'home-grant' but its scene carries a day-one-chip" in e for e in errors), mutation


def test_admin_home_moment_metadata_rejects_an_empty_spoken_noun() -> None:
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "const HOUSEHOLD_EXAMPLE_NOUNS={quiet:'evening',", "const HOUSEHOLD_EXAMPLE_NOUNS={quiet:'',"
    )
    errors = _admin_home_moment_errors(source)
    assert any("quiet is empty" in error for error in errors), errors


def test_admin_home_moment_metadata_rejects_a_missing_chip_and_a_missing_scene() -> None:
    template = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    stripped = template.replace(' <em class="day-one-chip">day one</em>', "")
    assert any("carries no day-one-chip" in error for error in _admin_home_moment_errors(stripped))

    start = template.index('<div class="listening-invitation household-scene" data-explainer-scenario="quiet"')
    end = template.rindex(
        '<div class="listening-invitation household-scene"', 0, template.index('data-explainer-scenario="laundry"')
    )
    errors = _admin_home_moment_errors(template[:start] + template[end:])
    assert any("scenes are missing: quiet" in error for error in errors), errors


def test_admin_home_moment_metadata_rejects_a_key_without_a_spoken_noun() -> None:
    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "const HOUSEHOLD_EXAMPLE_NOUNS={quiet:'evening',", "const HOUSEHOLD_EXAMPLE_NOUNS={"
    )
    errors = _admin_home_moment_errors(source)
    assert any("HOUSEHOLD_EXAMPLE_NOUNS is missing: quiet" in error for error in errors), errors


def test_admin_metadata_rejects_a_home_moment_shadowing_a_narration_clip(tmp_path: Path) -> None:
    """toggleFirstListenGuide reads HOUSEHOLD_EXAMPLES before FIRST_LISTEN_GUIDES.

    A shared key silently re-points a narration button at a home moment. The two
    maps are validated against disjoint manifests, so neither can see the other's
    keys and only the cross-check catches it.
    """

    source = VALIDATOR.ADMIN_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "quiet:{file:'quiet.mp3',version:'02fc7d83734a'}",
        "privacy:{file:'quiet.mp3',version:'02fc7d83734a'}",
    )
    template_path = tmp_path / "admin.html"
    template_path.write_text(source, encoding="utf-8")

    # The runtime shadow is JS-vs-JS, so the JS rename alone must be enough; the
    # manifest is deliberately left untouched.
    home_manifest = json.loads((SHIPPED_AUDIO_ROOT / "home_moments" / "spoken_assets.json").read_text(encoding="utf-8"))
    guide_manifest = json.loads((SHIPPED_AUDIO_ROOT / "spoken_assets.json").read_text(encoding="utf-8"))

    errors = VALIDATOR._validate_admin_guide_metadata(
        guide_manifest,
        voice_manifest=_free_manifest(),
        home_moment_manifest=home_manifest,
        admin_template_path=template_path,
    )
    assert any("share keys: privacy" in error for error in errors), errors


@pytest.mark.asyncio
async def test_home_moment_clips_are_public_audio_mpeg() -> None:
    from mammamiradio.web.streamer import router

    app = FastAPI()
    app.include_router(router)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        for name in HOME_MOMENTS:
            response = await client.get(f"/static/audio/home_moments/{name}")
            assert response.status_code == 200, name
            assert response.headers["content-type"].startswith("audio/mpeg"), name
            assert response.content == (HOME_MOMENT_ROOT / name).read_bytes()


@pytest.mark.parametrize("default_root", [False, True])
@pytest.mark.parametrize("failure", [None, "inventory", "hash", "refresh_voice", "render", "validation", "publication"])
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
    elif failure == "refresh_voice":
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
    if failure == "publication":
        publish = GENERATOR._publish_staged_file

        def fail_manifest(source, destination):
            if destination.name == "spoken_assets.json":
                raise RuntimeError("manifest publication failed")
            publish(source, destination)

        monkeypatch.setattr(GENERATOR, "_publish_staged_file", fail_manifest)
    monkeypatch.setattr(
        GENERATOR.runpy,
        "run_path",
        lambda _path: {
            "DEMO_SPOKEN_PATHS": VALIDATOR.DEMO_SPOKEN_PATHS,
            "validate_spoken_asset_manifest": VALIDATOR.validate_spoken_asset_manifest,
            "validate_demo_spoken_assets": lambda **kwargs: (
                ["invalid staged media"]
                if failure == "validation" and kwargs.get("include_admin_opening", True)
                else VALIDATOR.validate_spoken_asset_manifest(assets_root=kwargs["assets_root"])
            ),
        },
    )
    args = argparse.Namespace(env_file=root / "absent.env", output_root=root, clip=None, station_opening=True)
    if failure and failure != "refresh_voice":
        with pytest.raises(RuntimeError):
            asyncio.run(GENERATOR._run(args))
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
        assert len(calls) == (0 if failure in {"inventory", "hash"} else 1)
    else:
        asyncio.run(GENERATOR._run(args))
        assert calls == ["first_listen_admin_show"]
        after = json.loads((root / "spoken_assets.json").read_text())
        opening = next(entry for entry in after["assets"] if entry["path"] == VALIDATOR.ADMIN_STATION_OPENING_PATH)
        assert opening["canonical_render_receipt"] == VALIDATOR._canonical_render_receipt()
        for entry in original["assets"]:
            if not entry["path"].endswith("first_listen_admin_show.mp3"):
                assert entry in after["assets"]
                assert (root / entry["path"]).read_bytes() == before[Path(entry["path"])]
    assert all(p.read_bytes() == payload for p, payload in browser_before.items())


@pytest.mark.requires_ffmpeg
def test_station_render_rejects_hash_approved_non_audio_before_paid_synthesis(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    shutil.copytree(GENERATOR.STATION_OUTPUT_ROOT, root)
    manifest_path = root / "spoken_assets.json"
    manifest = json.loads(manifest_path.read_text())
    retained = next(entry for entry in manifest["assets"] if entry["path"].startswith("banter/"))
    (root / retained["path"]).write_bytes(b"not an audio recording")
    retained["sha256"] = GENERATOR._sha256(root / retained["path"])
    manifest_path.write_text(json.dumps(manifest))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    async def unexpected_render(*args, **kwargs):
        pytest.fail("invalid retained media reached paid synthesis")

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-no-provider-calls")
    monkeypatch.setattr(GENERATOR, "_render_clip", unexpected_render)
    args = argparse.Namespace(env_file=root / "absent.env", output_root=root, clip=None, station_opening=True)
    with pytest.raises(RuntimeError, match="invalid station pack:.*" + retained["path"]):
        asyncio.run(GENERATOR._run(args))
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


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


@pytest.mark.parametrize("failure_index", [0, 1, 2])
@pytest.mark.parametrize("existing", [True, False])
def test_pack_publication_failure_restores_every_destination(tmp_path, monkeypatch, failure_index, existing):
    staged, output = tmp_path / "staged", tmp_path / "output"
    staged.mkdir()
    output.mkdir()
    files = []
    for name in ("privacy.mp3", "ai.mp3", "spoken_assets.json"):
        (staged / name).write_bytes(b"new " + name.encode())
        if existing:
            (output / name).write_bytes(b"old " + name.encode())
            (output / name).chmod(0o640)
        files.append((staged / name, output / name))
    before = {p.name: (p.read_bytes(), p.stat().st_mode & 0o777) for p in output.iterdir()}
    publish = GENERATOR._publish_staged_file
    calls = 0

    def fail_once(source, destination):
        nonlocal calls
        index, calls = calls, calls + 1
        if index == failure_index:
            raise OSError("injected publication failure")
        publish(source, destination)

    monkeypatch.setattr(GENERATOR, "_publish_staged_file", fail_once)
    with pytest.raises(OSError, match="injected publication failure"):
        GENERATOR._publish_staged_pack(files)
    assert {p.name: (p.read_bytes(), p.stat().st_mode & 0o777) for p in output.iterdir()} == before


@pytest.mark.parametrize(
    "clip, pause, bed",
    [
        (GENERATOR.STATION_OPENING_CLIP, 600, 10.4),
        (GENERATOR.GUIDE_CLIPS[0], 280, 3.0),
        (GENERATOR.GUIDE_CLIPS[1], 280, None),
    ],
)
@pytest.mark.parametrize("duration", [None, 3.99, 4.0, 20.0, 20.01])
def test_render_clip_constructs_dialogue_and_enforces_duration(tmp_path, monkeypatch, clip, pause, bed, duration):
    from mammamiradio.audio import normalizer

    calls = []
    destination = tmp_path / "finished.mp3"

    async def render_line(host, text, path):
        path.write_bytes(text.encode())
        return path

    def concat(paths, output, **kwargs):
        assert [path.read_text() for path in paths] == [line.text for line in clip.lines]
        assert kwargs == {"silence_ms": pause, "loudnorm": bed is None, "strict_duration": True}
        output.write_bytes(b"dialogue")

    def generate_bed(path, seconds, notes):
        calls.append((seconds, notes))
        path.write_bytes(b"bed")

    def mix(voice, sting, output):
        output.write_bytes(voice.read_bytes() + sting.read_bytes())

    monkeypatch.setattr(GENERATOR, "_render_line", render_line)
    monkeypatch.setattr(normalizer, "concat_files", concat)
    monkeypatch.setattr(normalizer, "generate_station_id_bed", generate_bed)
    monkeypatch.setattr(normalizer, "mix_voice_with_sting", mix)
    monkeypatch.setattr(normalizer, "probe_duration_sec", lambda path: duration if path == destination else 10.0)
    render = GENERATOR._render_clip(
        clip, {line.host: line.host for line in clip.lines}, tmp_path, destination, motif_notes=[60, 64]
    )
    if duration is None or not 4 <= duration <= 20:
        with pytest.raises(RuntimeError, match="between 4 and 20 seconds"):
            asyncio.run(render)
    else:
        entry = asyncio.run(render)
        assert entry["duration_seconds"] == duration
        assert entry["sha256"] == GENERATOR._sha256(destination)
        assert entry["transcript"] == clip.transcript
    assert calls == ([(bed, [60, 64])] if bed is not None else [])
    assert destination.read_bytes() == (b"dialoguebed" if bed is not None else b"dialogue")

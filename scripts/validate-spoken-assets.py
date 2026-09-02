#!/usr/bin/env python3
"""Validate packaged spoken-audio inventory, hashes, and transcripts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+ uses the stdlib module.
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_manifest_validator():
    """Load the shared validator under the system Python used by shell gates."""

    if sys.version_info < (3, 10):  # noqa: UP036 - shell release gates may use macOS Python 3.9.
        # The repository targets Python 3.11+, but macOS shell gates can resolve
        # /usr/bin/python3 (3.9). The shared module is otherwise stdlib-only;
        # temporarily ignore the newer dataclass slots option while importing it.
        import dataclasses

        stdlib_dataclass = dataclasses.dataclass

        def compatible_dataclass(_cls=None, /, **kwargs):
            kwargs.pop("slots", None)
            return stdlib_dataclass(_cls, **kwargs)

        dataclasses.dataclass = compatible_dataclass
        try:
            from mammamiradio.core.spoken_assets import validate_spoken_asset_manifest
        finally:
            dataclasses.dataclass = stdlib_dataclass
        return validate_spoken_asset_manifest

    from mammamiradio.core.spoken_assets import validate_spoken_asset_manifest

    return validate_spoken_asset_manifest


validate_spoken_asset_manifest = _load_manifest_validator()

DEMO_ASSETS_ROOT = REPO_ROOT / "mammamiradio" / "assets" / "demo"
STATIC_ROOT = REPO_ROOT / "mammamiradio" / "web" / "static"
BROWSER_AUDIO_ROOT = STATIC_ROOT / "audio"
RADIO_CONFIG_PATH = REPO_ROOT / "radio.toml"
ADMIN_TEMPLATE_PATH = REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"
_ELEVENLABS_DEFAULT_VOICE_SETTINGS: dict[str, float | bool] = {
    "stability": 0.42,
    "similarity_boost": 0.78,
    "style": 0.45,
    "use_speaker_boost": True,
}
_ELEVENLABS_V2_MODEL = "eleven_multilingual_v2"
_ELEVENLABS_V3_MODEL = "eleven_v3"
_ELEVENLABS_SUPPORTED_MODELS = frozenset({_ELEVENLABS_V2_MODEL, _ELEVENLABS_V3_MODEL})
_ELEVENLABS_DELIVERY_PROFILES = frozenset({"none", "marco", "giulia"})
_PY39_NUMERIC_TYPES = (int, float)
BROWSER_GUIDE_MAX_BYTES = 5 * 1024 * 1024 // 2
BROWSER_GUIDE_CODEC = "mp3"
BROWSER_GUIDE_SAMPLE_RATE_HZ = 48_000
BROWSER_GUIDE_CHANNELS = 2
BROWSER_GUIDE_CHANNEL_LAYOUT = "stereo"
BROWSER_GUIDE_BITRATE_BPS = 192_000
BROWSER_GUIDE_MIN_DURATION_SECONDS = 4.0
BROWSER_GUIDE_MAX_DURATION_SECONDS = 20.0
BROWSER_GUIDE_DURATION_TOLERANCE_SECONDS = 0.05
BROWSER_GUIDE_MIN_LUFS = -19.5
BROWSER_GUIDE_MAX_LUFS = -14.0
BROWSER_GUIDE_MAX_TRUE_PEAK_DBTP = -1.0
BROWSER_GUIDE_PATHS = (
    "first_listen/welcome.mp3",
    "first_listen/sound-check.mp3",
    "first_listen/not-yet.mp3",
    "first_listen/receipt-recovery.mp3",
    "first_listen/privacy.mp3",
    "first_listen/ai.mp3",
    "first_listen/success.mp3",
)
BROWSER_GUIDE_HOSTS = ("Marco", "Giulia")
DEMO_BANTER_CODEC = "mp3"
DEMO_BANTER_SAMPLE_RATE_HZ = 48_000
DEMO_BANTER_CHANNELS = 2
DEMO_BANTER_CHANNEL_LAYOUT = "stereo"
DEMO_BANTER_BITRATE_BPS = 192_000
DEMO_BANTER_MIN_DURATION_SECONDS = 20.0
DEMO_BANTER_MAX_DURATION_SECONDS = 120.0
DEMO_BANTER_MIN_LUFS = -19.5
DEMO_BANTER_MAX_LUFS = -14.0
DEMO_BANTER_MAX_TRUE_PEAK_DBTP = -1.0
DEMO_BANTER_MAX_BYTES = 40 * 1024 * 1024


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate packaged spoken-audio inventory, hashes, and transcripts.",
    )
    roots = parser.add_mutually_exclusive_group()
    roots.add_argument(
        "--assets-root",
        type=Path,
        help=(
            "validate only this assets directory containing spoken_assets.json; "
            "the default validates every shipped spoken-audio inventory"
        ),
    )
    roots.add_argument(
        "--browser-assets-root",
        type=Path,
        help="validate this browser narration root with the full First Listen release contract",
    )
    parser.add_argument("--static-root", type=Path, help="static root paired with --browser-assets-root")
    parser.add_argument("--admin-template", type=Path, help="admin template paired with --browser-assets-root")
    parser.add_argument("--radio-config", type=Path, help="radio.toml paired with --browser-assets-root")
    return parser.parse_args()


def _read_manifest(assets_root: Path) -> dict[str, object] | None:
    try:
        payload = json.loads((assets_root / "spoken_assets.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _canonical_voice_settings(
    *,
    host_name: str,
    model_id: str,
    raw_settings: object,
) -> dict[str, object] | None:
    """Resolve provider settings without importing the runtime TTS stack."""

    if raw_settings is None:
        settings: dict[str, object] = {}
    elif isinstance(raw_settings, dict):
        settings = dict(raw_settings)
    else:
        raise ValueError(f"{host_name} voice_settings must be a TOML table")

    if model_id == _ELEVENLABS_V2_MODEL:
        # Keep key order and override behavior aligned with audio.tts's historic
        # ElevenLabs v2 payload. The manifest records this exact provider input.
        return {**_ELEVENLABS_DEFAULT_VOICE_SETTINGS, **settings}
    if model_id != _ELEVENLABS_V3_MODEL:
        raise ValueError(f"{host_name} uses unsupported ElevenLabs model {model_id!r}")
    if not settings:
        return None
    if set(settings) != {"stability"}:
        raise ValueError(f"{host_name} ElevenLabs V3 voice_settings may contain only stability")
    stability = settings["stability"]
    if isinstance(stability, bool) or not isinstance(stability, _PY39_NUMERIC_TYPES) or not math.isfinite(stability):
        raise ValueError(f"{host_name} ElevenLabs V3 stability must be a finite number between 0 and 1")
    if not 0 <= stability <= 1:
        raise ValueError(f"{host_name} ElevenLabs V3 stability must be between 0 and 1")
    return {"stability": float(stability)}


def _canonical_host_receipt(name: str, raw_host: dict[str, object]) -> dict[str, object]:
    """Parse one receipt host using the runtime config and TTS defaults."""

    engine = raw_host.get("engine", "edge")
    if not isinstance(engine, str):
        raise ValueError(f"{name} engine must be a string")
    if (engine or "edge").strip().lower() != "elevenlabs":
        raise ValueError(f"{name} is not configured with the canonical ElevenLabs provider")

    voice_id = raw_host.get("voice")
    if not isinstance(voice_id, str) or not voice_id:
        raise ValueError(f"{name} must configure a canonical ElevenLabs voice ID")

    raw_model = raw_host.get("elevenlabs_model")
    if raw_model is None:
        model_id = _ELEVENLABS_V2_MODEL
    elif not isinstance(raw_model, str) or not raw_model.strip():
        raise ValueError(f"{name} elevenlabs_model must be a non-empty string")
    else:
        model_id = raw_model.strip()
    if model_id not in _ELEVENLABS_SUPPORTED_MODELS:
        raise ValueError(f"{name} uses unsupported ElevenLabs model {model_id!r}")

    raw_profile = raw_host.get("delivery_profile")
    if raw_profile is None:
        delivery_profile = "none"
    elif not isinstance(raw_profile, str) or not raw_profile.strip():
        raise ValueError(f"{name} delivery_profile must be a non-empty string")
    else:
        delivery_profile = raw_profile.strip().lower()
    if delivery_profile not in _ELEVENLABS_DELIVERY_PROFILES:
        raise ValueError(f"{name} uses unsupported delivery_profile {delivery_profile!r}")
    if delivery_profile != "none" and delivery_profile != name.casefold():
        raise ValueError(f"{name} cannot use the {delivery_profile!r} delivery profile")
    if model_id == _ELEVENLABS_V3_MODEL and delivery_profile == "none":
        raise ValueError(f"{name} ElevenLabs V3 requires its audited delivery profile")
    if model_id != _ELEVENLABS_V3_MODEL and delivery_profile != "none":
        raise ValueError(f"{name} delivery_profile requires ElevenLabs V3")

    return {
        "name": name,
        "voice_id": voice_id,
        "model_id": model_id,
        "voice_settings": _canonical_voice_settings(
            host_name=name,
            model_id=model_id,
            raw_settings=raw_host.get("voice_settings", {}),
        ),
        "delivery_profile": delivery_profile,
    }


def _canonical_render_receipt(config_path: Path = RADIO_CONFIG_PATH) -> dict[str, object]:
    """Return the exact non-secret canonical voice inputs from radio.toml."""

    with Path(config_path).open("rb") as handle:
        config = tomllib.load(handle)
    raw_hosts = config.get("hosts", [])
    if not isinstance(raw_hosts, list):
        raise ValueError("radio.toml hosts must be an array of tables")
    hosts: dict[str, dict[str, object]] = {}
    for raw_host in raw_hosts:
        if not isinstance(raw_host, dict):
            raise ValueError("radio.toml hosts must contain only tables")
        host_name = raw_host.get("name")
        if host_name in BROWSER_GUIDE_HOSTS:
            hosts[str(host_name)] = raw_host
    if set(hosts) != set(BROWSER_GUIDE_HOSTS):
        raise ValueError("radio.toml must configure both Marco and Giulia")

    return {
        "schema_version": 1,
        "source": "radio.toml",
        "provider": "elevenlabs",
        "fallback": False,
        "hosts": [_canonical_host_receipt(name, hosts[name]) for name in BROWSER_GUIDE_HOSTS],
    }


def _probe_audio(path: Path, *, ffprobe: str) -> tuple[dict[str, object] | None, str | None]:
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,codec_type,sample_rate,channels,channel_layout,bit_rate:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return None, "ffprobe timed out"
    except OSError as exc:
        return None, f"ffprobe could not run: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"ffprobe exit {result.returncode}"
        return None, f"not ffprobe-readable audio ({detail})"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"ffprobe returned invalid JSON ({exc})"
    streams = payload.get("streams") if isinstance(payload, dict) else None
    format_data = payload.get("format") if isinstance(payload, dict) else None
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        return None, "ffprobe did not return exactly one selected audio stream"
    if not isinstance(format_data, dict):
        return None, "ffprobe did not return format metadata"
    return {"stream": streams[0], "format": format_data}, None


def _measure_loudness(path: Path, *, ffmpeg: str) -> tuple[tuple[float, float] | None, str | None]:
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-af",
                "ebur128=peak=true",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None, "ffmpeg loudness measurement timed out"
    except OSError as exc:
        return None, f"ffmpeg loudness measurement could not run: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exit {result.returncode}"
        return None, f"ffmpeg loudness measurement failed ({detail})"

    integrated_matches = re.findall(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", result.stderr)
    peak_matches = re.findall(r"Peak:\s+(-?\d+(?:\.\d+)?)\s+dBFS", result.stderr)
    if not integrated_matches or not peak_matches:
        return None, "ffmpeg did not report integrated loudness and true peak"
    return (float(integrated_matches[-1]), float(peak_matches[-1])), None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _resource_sha256(resource: Any) -> str:
    digest = hashlib.sha256()
    with resource.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_demo_spoken_assets(
    *,
    assets_root: Path = DEMO_ASSETS_ROOT,
    package_assets_root: Any = None,
) -> list[str]:
    """Validate every reviewed banter byte as shipped, decodable package audio."""

    root = Path(assets_root)
    errors = validate_spoken_asset_manifest(assets_root=root)
    manifest = _read_manifest(root)
    if manifest is None:
        return errors
    raw_assets = manifest.get("assets")
    if not isinstance(raw_assets, list):
        return errors
    banter_entries = [
        entry
        for entry in raw_assets
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and str(entry["path"]).startswith("banter/")
    ]
    if not banter_entries:
        errors.append("demo banter inventory is empty")
        return errors

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        errors.append("ffprobe is required to validate packaged demo banter")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        errors.append("ffmpeg is required to validate packaged demo banter loudness")

    if package_assets_root is None:
        try:
            package_assets_root = resources.files("mammamiradio").joinpath("assets").joinpath("demo")
        except (ModuleNotFoundError, TypeError) as exc:
            errors.append(f"cannot resolve packaged demo assets: {exc}")

    total_bytes = 0
    for entry in banter_entries:
        relative_path = str(entry["path"])
        asset_path = root / relative_path
        declared_sha256 = entry.get("sha256")
        try:
            total_bytes += asset_path.stat().st_size
        except OSError:
            pass

        if package_assets_root is not None:
            package_asset = package_assets_root
            for part in Path(relative_path).parts:
                package_asset = package_asset.joinpath(part)
            try:
                if not package_asset.is_file():
                    errors.append(f"{relative_path} is not reachable as a mammamiradio package resource")
                elif isinstance(declared_sha256, str) and _resource_sha256(package_asset) != declared_sha256:
                    errors.append(f"{relative_path} packaged resource sha256 does not match")
            except OSError as exc:
                errors.append(f"{relative_path} packaged resource is unreadable: {exc}")

        if ffprobe is None or not asset_path.is_file():
            continue
        media, probe_error = _probe_audio(asset_path, ffprobe=ffprobe)
        if probe_error is not None:
            joiner = " is " if probe_error.startswith("not ") else " "
            errors.append(f"{relative_path}{joiner}{probe_error}")
            continue
        assert media is not None
        stream = media["stream"]
        format_data = media["format"]
        assert isinstance(stream, dict)
        assert isinstance(format_data, dict)
        expected_stream = {
            "codec_type": "audio",
            "codec_name": DEMO_BANTER_CODEC,
            "sample_rate": DEMO_BANTER_SAMPLE_RATE_HZ,
            "channels": DEMO_BANTER_CHANNELS,
            "channel_layout": DEMO_BANTER_CHANNEL_LAYOUT,
            "bit_rate": DEMO_BANTER_BITRATE_BPS,
        }
        for field, expected in expected_stream.items():
            actual = stream.get(field)
            normalized = _number(actual) if isinstance(expected, int) else actual
            if normalized != expected:
                errors.append(f"{relative_path} {field} must be {expected!r}; got {actual!r}")
        duration = _number(format_data.get("duration"))
        if duration is None:
            errors.append(f"{relative_path} ffprobe duration is missing or invalid")
        elif not DEMO_BANTER_MIN_DURATION_SECONDS <= duration <= DEMO_BANTER_MAX_DURATION_SECONDS:
            errors.append(
                f"{relative_path} duration {duration:.3f}s is outside "
                f"{DEMO_BANTER_MIN_DURATION_SECONDS:.1f}-{DEMO_BANTER_MAX_DURATION_SECONDS:.1f}s"
            )
        if ffmpeg is not None:
            loudness, loudness_error = _measure_loudness(asset_path, ffmpeg=ffmpeg)
            if loudness_error is not None:
                errors.append(f"{relative_path} {loudness_error}")
            else:
                assert loudness is not None
                integrated_lufs, true_peak_dbtp = loudness
                if not DEMO_BANTER_MIN_LUFS <= integrated_lufs <= DEMO_BANTER_MAX_LUFS:
                    errors.append(
                        f"{relative_path} integrated loudness {integrated_lufs:.1f} LUFS is outside "
                        f"{DEMO_BANTER_MIN_LUFS:.1f} to {DEMO_BANTER_MAX_LUFS:.1f} LUFS"
                    )
                if true_peak_dbtp > DEMO_BANTER_MAX_TRUE_PEAK_DBTP:
                    errors.append(
                        f"{relative_path} true peak {true_peak_dbtp:.1f} dBTP exceeds "
                        f"{DEMO_BANTER_MAX_TRUE_PEAK_DBTP:.1f} dBTP"
                    )
    if total_bytes > DEMO_BANTER_MAX_BYTES:
        errors.append(f"demo banter bundle is {total_bytes} bytes; maximum is {DEMO_BANTER_MAX_BYTES} bytes (40 MiB)")
    return errors


def _browser_route_error(path: Path, *, relative_path: str, static_root: Path) -> str | None:
    """Mirror the static route's resolution contract for one browser asset."""

    public_relative = Path("audio") / relative_path
    if public_relative.is_absolute() or ".." in public_relative.parts:
        return "does not produce a safe /static/audio route path"
    try:
        resolved_static_root = static_root.resolve()
        resolved_asset = path.resolve()
        resolved_route = (static_root / public_relative).resolve()
    except (OSError, RuntimeError) as exc:
        return f"cannot resolve its /static/audio route path: {exc}"
    if (
        not resolved_route.is_relative_to(resolved_static_root)
        or resolved_route != resolved_asset
        or not resolved_route.is_file()
    ):
        return "does not resolve to this file through /static/audio"
    return None


class _GuideTranscriptParser(HTMLParser):
    """Collect plain-text transcripts from First Listen guide blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.guide_keys: set[str] = set()
        self.duplicate_keys: set[str] = set()
        self.transcripts: dict[str, str] = {}
        self.buttons: dict[str, list[dict[str, str | None]]] = {}
        self.orphan_buttons: list[dict[str, str | None]] = []
        self._div_depth = 0
        self._guide_root_depth = 0
        self._active_guide: str | None = None
        self._in_transcript = False
        self._collecting_paragraph = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "div":
            self._div_depth += 1
            guide_key = attributes.get("data-guide")
            if self._active_guide is None and "guide-audio" in classes and guide_key:
                if guide_key in self.guide_keys:
                    self.duplicate_keys.add(guide_key)
                self.guide_keys.add(guide_key)
                self._active_guide = guide_key
                self._guide_root_depth = self._div_depth
        elif tag == "details" and self._active_guide is not None and "guide-transcript" in classes:
            self._in_transcript = True
        elif tag == "p" and self._in_transcript and self._active_guide is not None:
            self._collecting_paragraph = True
            self._parts = []
        elif tag == "button" and "guide-audio-play" in classes:
            button = {
                "data-guide-key": attributes.get("data-guide-key"),
                "onclick": attributes.get("onclick"),
            }
            if self._active_guide is None:
                self.orphan_buttons.append(button)
            else:
                self.buttons.setdefault(self._active_guide, []).append(button)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._collecting_paragraph and self._active_guide is not None:
            transcript = " ".join("".join(self._parts).split())
            if self._active_guide in self.transcripts:
                self.duplicate_keys.add(self._active_guide)
            self.transcripts[self._active_guide] = transcript
            self._collecting_paragraph = False
            self._parts = []
        elif tag == "details" and self._in_transcript:
            self._in_transcript = False
        elif tag == "div":
            if self._active_guide is not None and self._div_depth == self._guide_root_depth:
                self._active_guide = None
                self._guide_root_depth = 0
                self._in_transcript = False
                self._collecting_paragraph = False
                self._parts = []
            self._div_depth = max(0, self._div_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._collecting_paragraph:
            self._parts.append(data)


_GUIDE_MAP_PATTERN = re.compile(r"const\s+FIRST_LISTEN_GUIDES\s*=\s*\{(?P<body>.*?)\};", re.DOTALL)
_GUIDE_ENTRY_PATTERN = re.compile(
    r"(?:'(?P<quoted_key>[^']+)'|(?P<plain_key>[A-Za-z][A-Za-z0-9_-]*))\s*:\s*"
    r"\{\s*file\s*:\s*'(?P<file>[^']+)'\s*,\s*version\s*:\s*'(?P<version>[^']+)'\s*\}"
)


def _validate_admin_guide_metadata(
    manifest: dict[str, object],
    *,
    admin_template_path: Path = ADMIN_TEMPLATE_PATH,
) -> list[str]:
    """Keep the admin's cache keys and visible transcripts bound to the manifest."""

    try:
        source = Path(admin_template_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read admin guide metadata from {admin_template_path}: {exc}"]

    raw_assets = manifest.get("assets")
    if not isinstance(raw_assets, list):
        return []  # The shared manifest validator reports the malformed inventory.
    expected: dict[str, dict[str, object]] = {}
    for raw_entry in raw_assets:
        if not isinstance(raw_entry, dict):
            continue
        relative_path = raw_entry.get("path")
        if not isinstance(relative_path, str) or not relative_path.startswith("first_listen/"):
            continue
        expected[Path(relative_path).stem] = raw_entry

    errors: list[str] = []
    map_match = _GUIDE_MAP_PATTERN.search(source)
    guide_entries: dict[str, dict[str, str]] = {}
    if map_match is None:
        errors.append("admin FIRST_LISTEN_GUIDES map is missing")
    else:
        body = map_match.group("body")
        for match in _GUIDE_ENTRY_PATTERN.finditer(body):
            key = match.group("quoted_key") or match.group("plain_key")
            if key in guide_entries:
                errors.append(f"admin FIRST_LISTEN_GUIDES contains duplicate key {key}")
            guide_entries[key] = {"file": match.group("file"), "version": match.group("version")}
        residual = _GUIDE_ENTRY_PATTERN.sub("", body).strip(" \t\r\n,")
        if residual:
            errors.append("admin FIRST_LISTEN_GUIDES contains unrecognized metadata")

    expected_keys = set(expected)
    declared_keys = set(guide_entries)
    missing_guides = sorted(expected_keys - declared_keys)
    unexpected_guides = sorted(declared_keys - expected_keys)
    if missing_guides:
        errors.append(f"admin FIRST_LISTEN_GUIDES is missing: {', '.join(missing_guides)}")
    if unexpected_guides:
        errors.append(f"admin FIRST_LISTEN_GUIDES has unexpected keys: {', '.join(unexpected_guides)}")

    for key in sorted(expected_keys & declared_keys):
        manifest_entry = expected[key]
        relative_path = manifest_entry.get("path")
        sha256 = manifest_entry.get("sha256")
        assert isinstance(relative_path, str)
        expected_file = Path(relative_path).name
        if guide_entries[key]["file"] != expected_file:
            errors.append(
                f"admin guide {key} file {guide_entries[key]['file']!r} does not match manifest {expected_file!r}"
            )
        if isinstance(sha256, str):
            expected_version = sha256[:12]
            if guide_entries[key]["version"] != expected_version:
                errors.append(
                    f"admin guide {key} version {guide_entries[key]['version']!r} does not match "
                    f"manifest sha256 prefix {expected_version!r}"
                )

    parser = _GuideTranscriptParser()
    parser.feed(source)
    parser.close()
    for key in sorted(parser.duplicate_keys):
        errors.append(f"admin guide transcript contains duplicate key {key}")
    missing_containers = sorted(expected_keys - parser.guide_keys)
    unexpected_containers = sorted(parser.guide_keys - expected_keys)
    if missing_containers:
        errors.append(f"admin guide containers are missing: {', '.join(missing_containers)}")
    if unexpected_containers:
        errors.append(f"admin guide containers have unexpected keys: {', '.join(unexpected_containers)}")
    if parser.orphan_buttons:
        errors.append("admin guide play buttons must be inside a guide-audio data-guide container")
    unexpected_button_containers = sorted(set(parser.buttons) - expected_keys)
    if unexpected_button_containers:
        errors.append(
            f"admin guide play buttons have unrecognized container keys: {', '.join(unexpected_button_containers)}"
        )
    for key in sorted(expected_keys):
        buttons = parser.buttons.get(key, [])
        if not buttons:
            errors.append(f"admin guide {key} must contain exactly one guide-audio-play button; found 0")
            continue
        if len(buttons) != 1:
            errors.append(f"admin guide {key} must contain exactly one guide-audio-play button; found {len(buttons)}")
        expected_onclick = f"toggleFirstListenGuide('{key}',this)"
        for button in buttons:
            button_key = button["data-guide-key"]
            if button_key is None:
                errors.append(f"admin guide {key} play button is missing data-guide-key")
            elif button_key not in expected_keys:
                errors.append(f"admin guide {key} play button has unrecognized data-guide-key {button_key!r}")
            elif button_key != key:
                errors.append(
                    f"admin guide {key} play button data-guide-key {button_key!r} does not match its container"
                )
            onclick = button["onclick"]
            if onclick != expected_onclick:
                errors.append(f"admin guide {key} play button onclick {onclick!r} must be exactly {expected_onclick!r}")
    missing_transcripts = sorted(expected_keys - set(parser.transcripts))
    unexpected_transcripts = sorted(set(parser.transcripts) - expected_keys)
    if missing_transcripts:
        errors.append(f"admin guide transcripts are missing: {', '.join(missing_transcripts)}")
    if unexpected_transcripts:
        errors.append(f"admin guide transcripts have unexpected keys: {', '.join(unexpected_transcripts)}")
    for key in sorted(expected_keys & set(parser.transcripts)):
        transcript = expected[key].get("transcript")
        if isinstance(transcript, str) and parser.transcripts[key] != " ".join(transcript.split()):
            errors.append(f"admin guide {key} transcript does not match spoken_assets.json")
    return errors


def validate_browser_narration_pack(
    *,
    assets_root: Path = BROWSER_AUDIO_ROOT,
    static_root: Path = STATIC_ROOT,
    radio_config_path: Path = RADIO_CONFIG_PATH,
    admin_template_path: Path = ADMIN_TEMPLATE_PATH,
) -> list[str]:
    """Validate the exact, bounded First Listen narration pack served to browsers."""

    root = Path(assets_root)
    errors = validate_spoken_asset_manifest(assets_root=root)
    manifest = _read_manifest(root)
    if manifest is None:
        return errors
    if manifest.get("bundle") != "first-listen-guide":
        errors.append("browser narration manifest bundle must be first-listen-guide")
    if manifest.get("render_provider") != "canonical":
        errors.append("browser narration render_provider must be canonical; fallback audio cannot ship")
    try:
        expected_receipt = _canonical_render_receipt(radio_config_path)
    except (OSError, ValueError) as exc:
        errors.append(f"cannot derive canonical render receipt from radio.toml: {exc}")
    else:
        if manifest.get("canonical_render_receipt") != expected_receipt:
            errors.append("canonical_render_receipt does not match the current Marco/Giulia radio.toml config")

    raw_assets = manifest.get("assets")
    if not isinstance(raw_assets, list):
        return errors
    errors.extend(_validate_admin_guide_metadata(manifest, admin_template_path=admin_template_path))

    entries_by_path: dict[str, dict[str, object]] = {}
    for raw_entry in raw_assets:
        if not isinstance(raw_entry, dict):
            continue
        relative_path = raw_entry.get("path")
        if isinstance(relative_path, str):
            entries_by_path[relative_path] = raw_entry

    expected = set(BROWSER_GUIDE_PATHS)
    declared = set(entries_by_path)
    missing = sorted(expected - declared)
    unexpected = sorted(declared - expected)
    if missing:
        errors.append(f"browser narration inventory is missing: {', '.join(missing)}")
    if unexpected:
        errors.append(f"browser narration inventory has unexpected clips: {', '.join(unexpected)}")

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        errors.append("ffprobe is required to validate the browser narration pack")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        errors.append("ffmpeg is required to measure browser narration loudness and true peak")

    total_bytes = 0
    for relative_path in BROWSER_GUIDE_PATHS:
        entry = entries_by_path.get(relative_path)
        if entry is None:
            continue
        transcript = entry.get("transcript")
        if isinstance(transcript, str):
            for host in BROWSER_GUIDE_HOSTS:
                if f"{host}:" not in transcript:
                    errors.append(f"{relative_path} transcript must include {host}")

        asset_path = root / relative_path
        try:
            total_bytes += asset_path.stat().st_size
        except OSError:
            pass  # The shared manifest validator reports missing or unreadable files.

        route_error = _browser_route_error(asset_path, relative_path=relative_path, static_root=Path(static_root))
        if route_error is not None:
            errors.append(f"{relative_path} {route_error}")
        if ffprobe is not None and asset_path.is_file():
            media, probe_error = _probe_audio(asset_path, ffprobe=ffprobe)
            if probe_error is not None:
                joiner = " is " if probe_error.startswith("not ") else " "
                errors.append(f"{relative_path}{joiner}{probe_error}")
            elif media is not None:
                stream = media["stream"]
                format_data = media["format"]
                assert isinstance(stream, dict)
                assert isinstance(format_data, dict)

                if stream.get("codec_type") != "audio":
                    errors.append(f"{relative_path} ffprobe stream must be audio")
                if stream.get("codec_name") != BROWSER_GUIDE_CODEC:
                    errors.append(
                        f"{relative_path} codec must be {BROWSER_GUIDE_CODEC}; got {stream.get('codec_name')!r}"
                    )
                if _number(stream.get("sample_rate")) != BROWSER_GUIDE_SAMPLE_RATE_HZ:
                    errors.append(
                        f"{relative_path} sample rate must be {BROWSER_GUIDE_SAMPLE_RATE_HZ} Hz; "
                        f"got {stream.get('sample_rate')!r}"
                    )
                if _number(stream.get("channels")) != BROWSER_GUIDE_CHANNELS:
                    errors.append(
                        f"{relative_path} channel count must be {BROWSER_GUIDE_CHANNELS}; "
                        f"got {stream.get('channels')!r}"
                    )
                if stream.get("channel_layout") != BROWSER_GUIDE_CHANNEL_LAYOUT:
                    errors.append(
                        f"{relative_path} channel layout must be {BROWSER_GUIDE_CHANNEL_LAYOUT}; "
                        f"got {stream.get('channel_layout')!r}"
                    )
                if _number(stream.get("bit_rate")) != BROWSER_GUIDE_BITRATE_BPS:
                    errors.append(
                        f"{relative_path} audio bitrate must be {BROWSER_GUIDE_BITRATE_BPS} bps; "
                        f"got {stream.get('bit_rate')!r}"
                    )

                actual_duration = _number(format_data.get("duration"))
                declared_duration = _number(entry.get("duration_seconds"))
                if actual_duration is None:
                    errors.append(f"{relative_path} ffprobe duration is missing or invalid")
                else:
                    if not BROWSER_GUIDE_MIN_DURATION_SECONDS <= actual_duration <= BROWSER_GUIDE_MAX_DURATION_SECONDS:
                        errors.append(
                            f"{relative_path} duration {actual_duration:.3f}s is outside "
                            f"{BROWSER_GUIDE_MIN_DURATION_SECONDS:.1f}-{BROWSER_GUIDE_MAX_DURATION_SECONDS:.1f}s"
                        )
                    if declared_duration is None:
                        errors.append(f"{relative_path} duration_seconds is missing or invalid")
                    elif abs(actual_duration - declared_duration) > BROWSER_GUIDE_DURATION_TOLERANCE_SECONDS:
                        errors.append(
                            f"{relative_path} duration_seconds {declared_duration:.3f}s does not match "
                            f"ffprobe {actual_duration:.3f}s"
                        )

        if ffmpeg is not None and asset_path.is_file():
            loudness, loudness_error = _measure_loudness(asset_path, ffmpeg=ffmpeg)
            if loudness_error is not None:
                errors.append(f"{relative_path} {loudness_error}")
            elif loudness is not None:
                integrated_lufs, true_peak_dbtp = loudness
                if not BROWSER_GUIDE_MIN_LUFS <= integrated_lufs <= BROWSER_GUIDE_MAX_LUFS:
                    errors.append(
                        f"{relative_path} integrated loudness {integrated_lufs:.1f} LUFS is outside "
                        f"{BROWSER_GUIDE_MIN_LUFS:.1f} to {BROWSER_GUIDE_MAX_LUFS:.1f} LUFS"
                    )
                if true_peak_dbtp > BROWSER_GUIDE_MAX_TRUE_PEAK_DBTP:
                    errors.append(
                        f"{relative_path} true peak {true_peak_dbtp:.1f} dBTP exceeds "
                        f"{BROWSER_GUIDE_MAX_TRUE_PEAK_DBTP:.1f} dBTP"
                    )

    if total_bytes > BROWSER_GUIDE_MAX_BYTES:
        errors.append(
            f"browser narration bundle is {total_bytes} bytes; maximum is {BROWSER_GUIDE_MAX_BYTES} bytes (2.5 MiB)"
        )
    return errors


def _is_demo_assets_root(assets_root: Path) -> bool:
    try:
        return assets_root.resolve() == DEMO_ASSETS_ROOT.resolve()
    except (OSError, RuntimeError):
        return False


def validate_requested_assets(assets_root: Path | None) -> list[str]:
    """Validate one custom root or every repository-owned spoken-audio inventory."""

    if assets_root is not None:
        if _is_demo_assets_root(assets_root):
            return validate_demo_spoken_assets(assets_root=assets_root)
        return validate_spoken_asset_manifest(assets_root=assets_root)

    errors: list[str] = []
    inventories = (
        (DEMO_ASSETS_ROOT, validate_demo_spoken_assets()),
        (BROWSER_AUDIO_ROOT, validate_browser_narration_pack()),
    )
    for root, root_errors in inventories:
        try:
            label = root.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            label = str(root)
        errors.extend(f"{label}: {error}" for error in root_errors)
    return errors


def main() -> int:
    args = _parse_args()
    if args.browser_assets_root is not None:
        errors = validate_browser_narration_pack(
            assets_root=args.browser_assets_root,
            static_root=args.static_root or STATIC_ROOT,
            radio_config_path=args.radio_config or RADIO_CONFIG_PATH,
            admin_template_path=args.admin_template or ADMIN_TEMPLATE_PATH,
        )
    else:
        errors = validate_requested_assets(args.assets_root)
    if errors:
        for error in errors:
            print(f"spoken-assets: {error}", file=sys.stderr)
        return 1
    if args.browser_assets_root is not None:
        print(
            "spoken-assets: browser manifest, canonical receipt, hashes, transcripts, admin metadata, "
            "media format, loudness, routes, and bundle size are valid"
        )
    elif args.assets_root is None:
        print(
            "spoken-assets: manifests, hashes, transcripts, demo package reachability/media format/loudness/bundle "
            "size, and browser canonical receipt/loudness/routes/bundle size are valid"
        )
    elif _is_demo_assets_root(args.assets_root):
        print(
            "spoken-assets: demo manifest, hashes, transcripts, package reachability, media format, loudness, "
            "and bundle size are valid"
        )
    else:
        print("spoken-assets: manifest, hashes, and transcripts are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

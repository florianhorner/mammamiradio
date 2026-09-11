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
FREE_VOICE_PATH = "voice_examples/free-voices.mp3"
FREE_VOICE_TRANSCRIPT = (
    "Marco: Mah… Giulia, somebody’s changed my microphone. My cousin says it’s the future. "
    "Giulia: Same us. Different voices. And your cousin still owes us the old microphone."
)
# Home moments are the Step 3 demo pack. They are byte copies of the explainer's
# rendered segments, so the explainer manifest stays the single source of truth and
# this validator binds to it rather than restating durations and hashes a third
# time. They keep their own budget and duration band: they are ~30-36s station
# segments, three times longer and ~1.7 MB heavier than the narration guides, and
# folding them into BROWSER_GUIDE_MAX_BYTES would overflow that budget instead of
# measuring it.
HOME_MOMENT_DIRNAME = "home_moments"
HOME_MOMENT_BUNDLE = "first-listen-home-moments"
HOME_MOMENT_SOURCE_ROOT = REPO_ROOT / "docs" / "explainer" / "public" / "audio"
HOME_MOMENT_SOURCE_MANIFEST = HOME_MOMENT_SOURCE_ROOT / "segments.manifest.json"
HOME_MOMENT_MAX_BYTES = 2 * 1024 * 1024
HOME_MOMENT_MIN_DURATION_SECONDS = 20.0
HOME_MOMENT_MAX_DURATION_SECONDS = 45.0
HOME_MOMENT_DURATION_TOLERANCE_SECONDS = 0.05
# "day-one" is reachable by a fresh install in narrow ambient context;
# "home-grant" needs household details it does not have. The vocabulary is the
# explainer's (docs/explainer/scenarios.mjs) and means the same thing here.
HOME_MOMENT_DAY_ONE = "day-one"
HOME_MOMENT_HOME_GRANT = "home-grant"
HOME_MOMENT_REACHABILITIES = frozenset({HOME_MOMENT_DAY_ONE, HOME_MOMENT_HOME_GRANT})
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
ADMIN_STATION_OPENING_PATH = "first_listen/first_listen_admin_show.mp3"
# Independent inventory: partial renders must not bless missing retained audio.
DEMO_SPOKEN_PATHS = (
    "recovery/continuity_1.mp3",
    "recovery/emergency_tone.mp3",
    "first_listen/first_listen_show.mp3",
    "banter/01-normal-espresso-machine-union.mp3",
    "banter/02-normal-corridor-case.mp3",
    "banter/03-normal-emergency-kit.mp3",
    "banter/04-normal-nico-button.mp3",
    "banter/05-normal-lonely-antenna.mp3",
    "banter/06-normal-long-time-coming.mp3",
    "banter/07-italian-cattaneo-scorecard.mp3",
    "banter/08-italian-archivio-scontrino.mp3",
    "banter/09-italian-microfono-geloso.mp3",
    "banter/10-italian-manuale-studio-b.mp3",
    "banter/11-italian-nico-cartolina.mp3",
    "banter/12-italian-dance-with-me.mp3",
    "banter/13-normal-cattaneo-scorecard-en.mp3",
    "banter/14-normal-archive-receipt-en.mp3",
    "banter/15-normal-jealous-microphone-en.mp3",
    "banter/16-normal-studio-b-manual-en.mp3",
    "banter/17-normal-nico-postcard-en.mp3",
    "banter/18-normal-dance-with-me-en.mp3",
    "banter/19-special-other-side.mp3",
    "banter/20-special-third-chair.mp3",
    "banter/21-special-not-a-test.mp3",
    "first_listen/first_listen_admin_show.mp3",
)


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
    include_admin_opening: bool = True,
) -> list[str]:
    """Validate packaged banter; exclude the old opening only before replacing it."""

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
    openings = [
        entry
        for entry in raw_assets
        if include_admin_opening and isinstance(entry, dict) and entry.get("path") == ADMIN_STATION_OPENING_PATH
    ]
    if include_admin_opening and _is_demo_assets_root(root) and len(openings) != 1:
        errors.append("demo inventory must contain the English Admin station opening")
    for entry in openings:
        if (
            entry.get("kind") != "speech"
            or entry.get("language") != "en"
            or entry.get("speakers") != ["Marco", "Giulia", "Marco", "Giulia"]
        ):
            errors.append("Admin station opening must retain its English Marco/Giulia dialogue")
        try:
            expected_receipt = _canonical_render_receipt(RADIO_CONFIG_PATH)
        except (OSError, ValueError) as exc:
            errors.append(f"cannot derive station opening canonical receipt: {exc}")
        else:
            if entry.get("canonical_render_receipt") != expected_receipt:
                errors.append("Admin station opening canonical_render_receipt does not match radio.toml")
    banter_entries.extend(openings)

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
        is_opening = relative_path == ADMIN_STATION_OPENING_PATH
        minimum = 15.0 if is_opening else DEMO_BANTER_MIN_DURATION_SECONDS
        maximum = 20.0 if is_opening else DEMO_BANTER_MAX_DURATION_SECONDS
        if duration is None:
            errors.append(f"{relative_path} ffprobe duration is missing or invalid")
        elif not minimum <= duration <= maximum:
            errors.append(f"{relative_path} duration {duration:.3f}s is outside {minimum:.1f}-{maximum:.1f}s")
        if is_opening:
            declared_duration = _number(entry.get("duration_seconds"))
            if (
                declared_duration is None
                or duration is None
                or abs(declared_duration - duration) > BROWSER_GUIDE_DURATION_TOLERANCE_SECONDS
            ):
                errors.append(f"{relative_path} declared duration does not match ffprobe")
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


class _HomeMomentSceneParser(HTMLParser):
    """Collect each Step 3 scene's declared reachability and whether it wears a chip.

    A regex over the ``<h5>`` was the first attempt and it was wrong: the chip is
    styled by class alone (``.first-listen-panel .day-one-chip`` in
    first-listen.css), so it renders identically from anywhere inside the scene.
    Moving it one line down into the caption defeated the check while a reader
    still saw "day one" on a gated moment. Walking the whole subtree is what
    makes the guard see what the reader sees.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scenes: dict = {}
        self.duplicate_keys: set = set()
        self.unkeyed_scenes = 0
        self._depth = 0
        self._active = None
        self._root_depth = 0
        self._in_quote = False
        self._quote_parts: list = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "div":
            self._depth += 1
            if self._active is None and "household-scene" in classes:
                key = attributes.get("data-explainer-scenario")
                if key is None:
                    self.unkeyed_scenes += 1
                    return
                if key in self.scenes:
                    self.duplicate_keys.add(key)
                self.scenes[key] = {
                    "reachability": attributes.get("data-reachability"),
                    "chipped": False,
                    "quote": None,
                }
                self._active = key
                self._root_depth = self._depth
                return
        if self._active is not None and "day-one-chip" in classes:
            self.scenes[self._active]["chipped"] = True
        # The pull quote is the first <span> in the scene; later spans belong to
        # the play-button copy inside .household-example.
        if tag == "span" and self._active is not None and self.scenes[self._active]["quote"] is None:
            self._in_quote = True
            self._quote_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._in_quote and self._active is not None:
            self.scenes[self._active]["quote"] = " ".join("".join(self._quote_parts).split())
            self._in_quote = False
            self._quote_parts = []
        if tag == "div":
            if self._active is not None and self._depth == self._root_depth:
                self._active = None
                self._root_depth = 0
                self._in_quote = False
                self._quote_parts = []
            self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._in_quote:
            self._quote_parts.append(data)


class _GuideTranscriptParser(HTMLParser):
    """Collect plain-text transcripts from one family of First Listen audio blocks.

    The container class, its key attribute and the play-button class are
    parameters so the home-moment pack is held to the same DOM contract as the
    narration guides instead of being validated by a near-copy of this class.
    """

    def __init__(
        self,
        *,
        container_class: str = "guide-audio",
        key_attribute: str = "data-guide",
        button_class: str = "guide-audio-play",
        button_key_attribute: str = "data-guide-key",
        toggle_function: str = "toggleFirstListenGuide",
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._container_class = container_class
        self._key_attribute = key_attribute
        self.button_class = button_class
        self.button_key_attribute = button_key_attribute
        self.toggle_function = toggle_function
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
            guide_key = attributes.get(self._key_attribute)
            if self._active_guide is None and self._container_class in classes and guide_key:
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
        elif tag == "button" and self.button_class in classes:
            button = {
                "key": attributes.get(self.button_key_attribute),
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
_HOME_MOMENT_MAP_PATTERN = re.compile(r"const\s+HOUSEHOLD_EXAMPLES\s*=\s*\{(?P<body>.*?)\};", re.DOTALL)
_HOME_MOMENT_NOUN_PATTERN = re.compile(r"const\s+HOUSEHOLD_EXAMPLE_NOUNS\s*=\s*\{(?P<body>[^}]*)\}")
_HOME_MOMENT_NOUN_ENTRY_PATTERN = re.compile(r"(?P<key>[A-Za-z][A-Za-z0-9_-]*)\s*:\s*'(?P<noun>[^']*)'")
_GUIDE_ENTRY_PATTERN = re.compile(
    r"(?:'(?P<quoted_key>[^']+)'|(?P<plain_key>[A-Za-z][A-Za-z0-9_-]*))\s*:\s*"
    r"\{\s*file\s*:\s*'(?P<file>[^']+)'\s*,\s*version\s*:\s*'(?P<version>[^']+)'\s*\}"
)


def _validate_audio_block_dom(
    source: str,
    expected: dict,
    *,
    parser: _GuideTranscriptParser,
    noun: str,
    container_hint: str,
) -> list[str]:
    """Bind one family of audio blocks in the admin template to its manifest.

    Container/button/transcript checks are identical for the narration guides and
    the home-moment pack; only the markup shape and the noun differ. Keeping one
    implementation is what stops a second pack from shipping with a weaker
    contract than the first.
    """

    errors: list[str] = []
    expected_keys = set(expected)
    parser.feed(source)
    parser.close()
    for key in sorted(parser.duplicate_keys):
        errors.append(f"admin {noun} transcript contains duplicate key {key}")
    missing_containers = sorted(expected_keys - parser.guide_keys)
    unexpected_containers = sorted(parser.guide_keys - expected_keys)
    if missing_containers:
        errors.append(f"admin {noun} containers are missing: {', '.join(missing_containers)}")
    if unexpected_containers:
        errors.append(f"admin {noun} containers have unexpected keys: {', '.join(unexpected_containers)}")
    if parser.orphan_buttons:
        errors.append(f"admin {noun} play buttons must be inside a {container_hint} container")
    unexpected_button_containers = sorted(set(parser.buttons) - expected_keys)
    if unexpected_button_containers:
        errors.append(
            f"admin {noun} play buttons have unrecognized container keys: {', '.join(unexpected_button_containers)}"
        )
    key_attribute = parser.button_key_attribute
    for key in sorted(expected_keys):
        buttons = parser.buttons.get(key, [])
        if not buttons:
            errors.append(f"admin {noun} {key} must contain exactly one {parser.button_class} button; found 0")
            continue
        if len(buttons) != 1:
            errors.append(
                f"admin {noun} {key} must contain exactly one {parser.button_class} button; found {len(buttons)}"
            )
        expected_onclick = f"{parser.toggle_function}('{key}',this)"
        for button in buttons:
            button_key = button["key"]
            if button_key is None:
                errors.append(f"admin {noun} {key} play button is missing {key_attribute}")
            elif button_key not in expected_keys:
                errors.append(f"admin {noun} {key} play button has unrecognized {key_attribute} {button_key!r}")
            elif button_key != key:
                errors.append(
                    f"admin {noun} {key} play button {key_attribute} {button_key!r} does not match its container"
                )
            onclick = button["onclick"]
            if onclick != expected_onclick:
                errors.append(
                    f"admin {noun} {key} play button onclick {onclick!r} must be exactly {expected_onclick!r}"
                )
    missing_transcripts = sorted(expected_keys - set(parser.transcripts))
    unexpected_transcripts = sorted(set(parser.transcripts) - expected_keys)
    if missing_transcripts:
        errors.append(f"admin {noun} transcripts are missing: {', '.join(missing_transcripts)}")
    if unexpected_transcripts:
        errors.append(f"admin {noun} transcripts have unexpected keys: {', '.join(unexpected_transcripts)}")
    for key in sorted(expected_keys & set(parser.transcripts)):
        transcript = expected[key].get("transcript")
        if isinstance(transcript, str) and parser.transcripts[key] != " ".join(transcript.split()):
            errors.append(f"admin {noun} {key} transcript does not match spoken_assets.json")
    return errors


def _parse_admin_audio_map(source: str, pattern: re.Pattern, label: str) -> tuple[dict, list[str]]:
    """Read one `{key: {file, version}}` map out of the admin template."""

    errors: list[str] = []
    entries: dict = {}
    map_match = pattern.search(source)
    if map_match is None:
        return entries, [f"admin {label} map is missing"]
    body = map_match.group("body")
    for match in _GUIDE_ENTRY_PATTERN.finditer(body):
        key = match.group("quoted_key") or match.group("plain_key")
        if key in entries:
            errors.append(f"admin {label} contains duplicate key {key}")
        entries[key] = {"file": match.group("file"), "version": match.group("version")}
    residual = _GUIDE_ENTRY_PATTERN.sub("", body).strip(" \t\r\n,")
    if residual:
        errors.append(f"admin {label} contains unrecognized metadata")
    return entries, errors


def _validate_admin_audio_map(
    entries: dict,
    expected: dict,
    *,
    label: str,
    noun: str,
) -> list[str]:
    """Bind a `{key: {file, version}}` map to its manifest inventory.

    `version` is the cache-bust token in the audio URL. Holding it to the
    manifest's sha256 prefix is what makes a regenerated clip impossible to ship
    behind a stale URL — the failure the hardcoded literals could not catch.
    """

    errors: list[str] = []
    expected_keys = set(expected)
    declared_keys = set(entries)
    missing = sorted(expected_keys - declared_keys)
    unexpected = sorted(declared_keys - expected_keys)
    if missing:
        errors.append(f"admin {label} is missing: {', '.join(missing)}")
    if unexpected:
        errors.append(f"admin {label} has unexpected keys: {', '.join(unexpected)}")
    for key in sorted(expected_keys & declared_keys):
        manifest_entry = expected[key]
        relative_path = manifest_entry.get("path")
        sha256 = manifest_entry.get("sha256")
        if not isinstance(relative_path, str):
            continue
        expected_file = Path(relative_path).name
        if entries[key]["file"] != expected_file:
            errors.append(f"admin {noun} {key} file {entries[key]['file']!r} does not match manifest {expected_file!r}")
        if isinstance(sha256, str):
            expected_version = sha256[:12]
            if entries[key]["version"] != expected_version:
                errors.append(
                    f"admin {noun} {key} version {entries[key]['version']!r} does not match "
                    f"manifest sha256 prefix {expected_version!r}"
                )
    return errors


def _validate_admin_guide_metadata(
    manifest: dict[str, object],
    *,
    voice_manifest: dict[str, object] | None = None,
    home_moment_manifest: dict[str, object] | None = None,
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
    voice_assets = voice_manifest.get("assets") if voice_manifest is not None else None
    if isinstance(voice_assets, list):
        for entry in voice_assets:
            if isinstance(entry, dict) and entry.get("path") == "free-voices.mp3":
                expected["free-voices"] = entry

    guide_entries, errors = _parse_admin_audio_map(source, _GUIDE_MAP_PATTERN, "FIRST_LISTEN_GUIDES")
    errors.extend(_validate_admin_audio_map(guide_entries, expected, label="FIRST_LISTEN_GUIDES", noun="guide"))

    errors.extend(
        _validate_audio_block_dom(
            source,
            expected,
            parser=_GuideTranscriptParser(),
            noun="guide",
            container_hint="guide-audio data-guide",
        )
    )
    home_errors, home_keys = _validate_admin_home_moment_metadata(source, home_moment_manifest)
    errors.extend(home_errors)
    # toggleFirstListenGuide reads HOUSEHOLD_EXAMPLES before FIRST_LISTEN_GUIDES, so a
    # shared key silently re-points a narration button at a home moment. The two maps
    # are validated against disjoint manifests, so only this check can see it.
    shared = sorted(set(guide_entries) & home_keys)
    if shared:
        errors.append(
            f"admin FIRST_LISTEN_GUIDES and HOUSEHOLD_EXAMPLES share keys: {', '.join(shared)}; "
            "the home moment would shadow the narration clip"
        )
    return errors


def _validate_admin_home_moment_metadata(source: str, manifest: dict | None) -> tuple[list[str], set]:
    """Hold the Step 3 demo pack to the same binding as the narration guides.

    Returns its errors plus the declared key set, so the caller can check the two
    audio maps do not shadow each other.
    """

    if manifest is None:
        return [], set()
    raw_assets = manifest.get("assets")
    if not isinstance(raw_assets, list):
        return [], set()  # validate_home_moments reports the malformed inventory.
    expected = {
        Path(entry["path"]).stem: entry
        for entry in raw_assets
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }

    entries, errors = _parse_admin_audio_map(source, _HOME_MOMENT_MAP_PATTERN, "HOUSEHOLD_EXAMPLES")
    errors.extend(_validate_admin_audio_map(entries, expected, label="HOUSEHOLD_EXAMPLES", noun="home moment"))
    errors.extend(
        _validate_audio_block_dom(
            source,
            expected,
            parser=_GuideTranscriptParser(
                container_class="household-example",
                key_attribute="data-home-moment",
                button_class="household-example-play",
                button_key_attribute="data-household-example",
                toggle_function="toggleHouseholdExample",
            ),
            noun="home moment",
            container_hint="household-example data-home-moment",
        )
    )

    errors.extend(_validate_home_moment_nouns(source, set(expected)))
    errors.extend(_validate_home_moment_reachability(source, expected))
    return errors, set(expected) | set(entries)


def _validate_home_moment_nouns(source: str, expected_keys: set) -> list[str]:
    """Every playable key needs a spoken noun, or its button label degrades silently.

    A key without one falls through to the generic "recording" in
    ``firstListenGuideLabel``, which reads as a narration clip rather than a
    scene. Nothing else notices.
    """

    match = _HOME_MOMENT_NOUN_PATTERN.search(source)
    if match is None:
        return ["admin HOUSEHOLD_EXAMPLE_NOUNS map is missing"]
    entries = {
        entry.group("key"): entry.group("noun")
        for entry in _HOME_MOMENT_NOUN_ENTRY_PATTERN.finditer(match.group("body"))
    }
    declared = set(entries)
    errors = [
        f"admin HOUSEHOLD_EXAMPLE_NOUNS {key} is empty; its button label would fall back to 'recording'"
        for key in sorted(key for key, noun in entries.items() if not noun.strip())
    ]
    missing = sorted(expected_keys - declared)
    unexpected = sorted(declared - expected_keys)
    if missing:
        errors.append(f"admin HOUSEHOLD_EXAMPLE_NOUNS is missing: {', '.join(missing)}")
    if unexpected:
        errors.append(f"admin HOUSEHOLD_EXAMPLE_NOUNS has unexpected keys: {', '.join(unexpected)}")
    return errors


def _validate_home_moment_reachability(source: str, expected: dict) -> list[str]:
    """Bind every scene's visible day-one boundary to the manifest.

    docs/explainer/scripts/build.mjs refuses to build a page that demonstrates
    only gated capability; Step 3 makes the same promise to a cold install and
    needs the same refusal.

    Presence alone is not enough, and an earlier version of this check made that
    mistake: it asked whether *a* day-one scene carried the chip and never asked
    whether a gated one carried it too. A ``home-grant`` scene wearing the chip
    tells a fresh install that the laundry works today, which narrow ambient
    context can never deliver -- the exact drift this guard exists to stop. So
    the binding runs both ways, per scene.
    """

    errors: list[str] = []
    manifest_reach = {key: entry.get("reachability") for key, entry in expected.items() if isinstance(entry, dict)}
    if HOME_MOMENT_DAY_ONE not in manifest_reach.values():
        errors.append(
            "admin home moments demonstrate only gated capability: no manifest entry is "
            f"reachability {HOME_MOMENT_DAY_ONE!r}"
        )

    parser = _HomeMomentSceneParser()
    parser.feed(source)
    parser.close()
    scenes = parser.scenes
    for _ in range(parser.unkeyed_scenes):
        errors.append("admin home moment scene is missing data-explainer-scenario")
    for key in sorted(parser.duplicate_keys):
        errors.append(f"admin home moment scene {key} is declared more than once")

    missing_scenes = sorted(set(manifest_reach) - set(scenes))
    unexpected_scenes = sorted(set(scenes) - set(manifest_reach))
    if missing_scenes:
        errors.append(f"admin home moment scenes are missing: {', '.join(missing_scenes)}")
    if unexpected_scenes:
        errors.append(f"admin home moment scenes have unexpected keys: {', '.join(unexpected_scenes)}")

    for key in sorted(set(manifest_reach) & set(scenes)):
        declared = scenes[key]["reachability"]
        chipped = scenes[key]["chipped"]
        manifest_value = manifest_reach[key]
        if declared != manifest_value:
            errors.append(
                f"admin home moment scene {key} declares data-reachability {declared!r} "
                f"but its manifest entry is {manifest_value!r}"
            )
            continue
        expected_quote = expected[key].get("quote") if isinstance(expected[key], dict) else None
        shown = scenes[key]["quote"]
        if isinstance(expected_quote, str):
            normalized = " ".join(expected_quote.split())
            if shown is None:
                errors.append(f"admin home moment {key} scene shows no pull quote")
            elif shown.strip('\u201c\u201d"') != normalized:
                errors.append(f"admin home moment {key} scene quote does not match its manifest quote")
        if manifest_value == HOME_MOMENT_DAY_ONE and not chipped:
            errors.append(f"admin home moment {key} is reachable today but its scene carries no day-one-chip")
        elif manifest_value != HOME_MOMENT_DAY_ONE and chipped:
            errors.append(
                f"admin home moment {key} is {manifest_value!r} but its scene carries a day-one-chip, "
                "promising a fresh install something it cannot reach"
            )
    return errors


def validate_browser_narration_pack(
    *,
    assets_root: Path = BROWSER_AUDIO_ROOT,
    static_root: Path = STATIC_ROOT,
    radio_config_path: Path = RADIO_CONFIG_PATH,
    admin_template_path: Path = ADMIN_TEMPLATE_PATH,
    staged_render: bool = False,
) -> list[str]:
    """Validate the pack; renders omit only bindings to the not-yet-updated UI."""

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
    side_root = root / "voice_examples"
    if not staged_render or side_root.exists() or side_root.is_symlink():
        errors.extend(
            validate_free_voice_example(
                assets_root=root,
                static_root=static_root,
                radio_config_path=radio_config_path,
                staged_render=staged_render,
            )
        )
    home_moment_root = root / HOME_MOMENT_DIRNAME
    if not staged_render or home_moment_root.exists() or home_moment_root.is_symlink():
        errors.extend(validate_home_moments(assets_root=root, static_root=static_root, staged_render=staged_render))
    if not staged_render:
        errors.extend(
            _validate_admin_guide_metadata(
                manifest,
                voice_manifest=_read_manifest(side_root),
                home_moment_manifest=_read_manifest(home_moment_root),
                admin_template_path=admin_template_path,
            )
        )

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

    return errors + _validate_browser_media(
        root=root,
        entries_by_path=entries_by_path,
        paths=BROWSER_GUIDE_PATHS,
        static_root=static_root,
        staged_render=staged_render,
    )


def _validate_browser_media(*, root, entries_by_path, paths, static_root, staged_render) -> list[str]:
    """Apply identical media limits to both packs without combining their provenance."""

    errors = []

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        errors.append("ffprobe is required to validate the browser narration pack")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        errors.append("ffmpeg is required to measure browser narration loudness and true peak")

    total_bytes = 0
    for relative_path in paths:
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

        if not staged_render:
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

    for relative_path in set((*BROWSER_GUIDE_PATHS, FREE_VOICE_PATH)) - set(paths):
        try:
            total_bytes += (root / relative_path).stat().st_size
        except OSError:
            pass
    if total_bytes > BROWSER_GUIDE_MAX_BYTES:
        errors.append(
            f"browser narration bundle is {total_bytes} bytes; maximum is {BROWSER_GUIDE_MAX_BYTES} bytes (2.5 MiB)"
        )
    return errors


def free_voice_render_receipt(config_path: Path = RADIO_CONFIG_PATH) -> dict[str, object]:
    with Path(config_path).open("rb") as handle:
        hosts = {host["name"]: host for host in tomllib.load(handle).get("hosts", [])}
    voices = []
    for name in BROWSER_GUIDE_HOSTS:
        voice = hosts.get(name, {}).get("edge_fallback_voice")
        if not isinstance(voice, str) or not voice.endswith("Neural"):
            raise ValueError(f"{name} must configure its free fallback voice")
        voices.append({"name": name, "voice_id": voice, "rate": "+0%", "pitch": "+0Hz"})
    return {"schema_version": 1, "source": "radio.toml", "provider": "edge", "hosts": voices}


def validate_free_voice_example(
    *, assets_root=BROWSER_AUDIO_ROOT, static_root=STATIC_ROOT, radio_config_path=RADIO_CONFIG_PATH, staged_render=False
) -> list[str]:
    from mammamiradio.core.path_safety import safe_path_within

    root = Path(assets_root)
    side_root = root / "voice_examples"
    paths = (side_root, side_root / "spoken_assets.json", root / FREE_VOICE_PATH)
    if any(safe_path_within(path, root, reject_symlinks=True) is None for path in paths):
        return ["free voice example path escapes its asset root or is a symlink"]
    manifest = _read_manifest(side_root)
    if manifest is None:
        return ["free voice example manifest is missing or unreadable"]
    errors = []
    if manifest.get("schema_version") != 1 or manifest.get("bundle") != "first-listen-free-voices":
        errors.append("free voice example schema or bundle is invalid")
    try:
        receipt = free_voice_render_receipt(radio_config_path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"cannot derive free voice receipt: {exc}")
    else:
        if manifest.get("render_provider") != "edge" or manifest.get("render_receipt") != receipt:
            errors.append("free voice example receipt does not match radio.toml")
    assets = manifest.get("assets")
    if (
        not isinstance(assets, list)
        or len(assets) != 1
        or not isinstance(assets[0], dict)
        or assets[0].get("path") != "free-voices.mp3"
    ):
        return [*errors, "free voice example inventory must contain only free-voices.mp3"]
    if {p.relative_to(side_root).as_posix() for p in side_root.rglob("*.mp3")} != {"free-voices.mp3"}:
        errors.append("free voice example inventory contains missing or unlisted audio")
    entry = assets[0]
    if (
        entry.get("kind") != "speech"
        or entry.get("language") != "en"
        or entry.get("speakers") != list(BROWSER_GUIDE_HOSTS)
        or entry.get("transcript") != FREE_VOICE_TRANSCRIPT
    ):
        errors.append("free voice example must retain its approved English Marco/Giulia dialogue")
    try:
        digest = hashlib.sha256((root / FREE_VOICE_PATH).read_bytes()).hexdigest()
        if entry.get("sha256") != digest:
            errors.append("free voice example sha256 does not match")
    except OSError:
        errors.append("free voice example audio is missing or unreadable")
    return errors + _validate_browser_media(
        root=root,
        entries_by_path={FREE_VOICE_PATH: entry},
        paths=(FREE_VOICE_PATH,),
        static_root=static_root,
        staged_render=staged_render,
    )


def validate_home_moments(
    *,
    assets_root: Path = BROWSER_AUDIO_ROOT,
    static_root: Path = STATIC_ROOT,
    staged_render: bool = False,
) -> list[str]:
    """Validate the Step 3 demo pack against the explainer segments it copies.

    The explainer already renders, hashes and audits these segments, so this pack
    is bound to that manifest rather than re-asserting durations and hashes a
    third time: drift in either direction fails here, and there is exactly one
    place to change a clip.

    Deliberately not routed through ``_validate_browser_media``: these are ~30-36s
    station segments, outside the 4-20s narration band, and their ~1.7 MB would
    consume the 2.5 MiB narration budget rather than be measured by it.
    """

    from mammamiradio.core.path_safety import safe_path_within

    root = Path(assets_root)
    side_root = root / HOME_MOMENT_DIRNAME
    if any(
        safe_path_within(path, root, reject_symlinks=True) is None
        for path in (side_root, side_root / "spoken_assets.json")
    ):
        return ["home moment path escapes its asset root or is a symlink"]

    manifest = _read_manifest(side_root)
    if manifest is None:
        return ["home moment manifest is missing or unreadable"]

    errors: list[str] = []
    if manifest.get("schema_version") != 1 or manifest.get("bundle") != HOME_MOMENT_BUNDLE:
        errors.append("home moment schema or bundle is invalid")

    raw_assets = manifest.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        errors.append("home moment manifest declares no assets")
        return errors

    try:
        source_manifest = json.loads(HOME_MOMENT_SOURCE_MANIFEST.read_text(encoding="utf-8"))
        source_segments = source_manifest["segments"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        errors.append(f"home moment source manifest {HOME_MOMENT_SOURCE_MANIFEST} is missing or unreadable")
        return errors

    declared: set = set()
    total_bytes = 0
    day_one_keys: list = []
    for entry in raw_assets:
        if not isinstance(entry, dict):
            errors.append("home moment manifest contains a malformed entry")
            continue
        relative_path = entry.get("path")
        if not isinstance(relative_path, str) or "/" in relative_path or not relative_path.endswith(".mp3"):
            errors.append(f"home moment path {relative_path!r} must be a bare .mp3 name")
            continue
        declared.add(relative_path)
        key = Path(relative_path).stem

        reachability = entry.get("reachability")
        if reachability not in HOME_MOMENT_REACHABILITIES:
            errors.append(
                f"home moment {key} reachability {reachability!r} must be one of {sorted(HOME_MOMENT_REACHABILITIES)}"
            )
        elif reachability == HOME_MOMENT_DAY_ONE:
            day_one_keys.append(key)

        asset_path = side_root / relative_path
        if safe_path_within(asset_path, root, reject_symlinks=True) is None:
            errors.append(f"home moment {relative_path} escapes its asset root or is a symlink")
            continue
        try:
            payload = asset_path.read_bytes()
        except OSError:
            errors.append(f"home moment {relative_path} is missing or unreadable")
            continue
        total_bytes += len(payload)

        digest = hashlib.sha256(payload).hexdigest()
        if entry.get("sha256") != digest:
            errors.append(f"home moment {relative_path} sha256 does not match its manifest entry")

        segment = source_segments.get(key) if isinstance(source_segments, dict) else None
        if not isinstance(segment, dict):
            errors.append(f"home moment {key} has no matching segment in {HOME_MOMENT_SOURCE_MANIFEST.name}")
        else:
            if segment.get("sha256") != digest:
                errors.append(f"home moment {relative_path} is not the explainer segment it claims to copy")
            # Hash the source file itself, not only its manifest. Without this the
            # chain is copy bytes -> copy manifest -> source manifest, so a source
            # that drifts while both manifests stay put passes clean and the pack
            # silently stops being a copy of anything.
            source_path = HOME_MOMENT_SOURCE_ROOT / relative_path
            if safe_path_within(source_path, HOME_MOMENT_SOURCE_ROOT, reject_symlinks=True) is None:
                errors.append(f"home moment source {relative_path} escapes its source root or is a symlink")
            else:
                try:
                    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
                except OSError:
                    errors.append(f"home moment source {relative_path} is missing or unreadable")
                else:
                    if source_digest != segment.get("sha256"):
                        errors.append(f"home moment source {relative_path} does not match its own segment manifest")
                    if source_digest != digest:
                        errors.append(f"home moment {relative_path} is not a byte copy of its source")
            source_duration = segment.get("durationSec")
            duration = entry.get("duration_seconds")
            if (
                isinstance(source_duration, _PY39_NUMERIC_TYPES)
                and isinstance(duration, _PY39_NUMERIC_TYPES)
                and abs(float(duration) - float(source_duration)) > HOME_MOMENT_DURATION_TOLERANCE_SECONDS
            ):
                errors.append(f"home moment {key} duration {duration} does not match the explainer's {source_duration}")

        duration = entry.get("duration_seconds")
        if not isinstance(duration, _PY39_NUMERIC_TYPES):
            errors.append(f"home moment {key} duration_seconds is missing or malformed")
        elif not HOME_MOMENT_MIN_DURATION_SECONDS <= float(duration) <= HOME_MOMENT_MAX_DURATION_SECONDS:
            errors.append(
                f"home moment {key} duration {duration}s is outside "
                f"{HOME_MOMENT_MIN_DURATION_SECONDS}-{HOME_MOMENT_MAX_DURATION_SECONDS}s"
            )

        transcript = entry.get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            errors.append(f"home moment {key} transcript is missing")

        quote = entry.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            # The scene comparison in _validate_home_moment_reachability only
            # runs when the manifest carries a quote, so an absent one would
            # silently unbind the line a reader actually reads.
            errors.append(f"home moment {key} quote is missing")

        if not staged_render:
            route_error = _browser_route_error(
                asset_path, relative_path=f"{HOME_MOMENT_DIRNAME}/{relative_path}", static_root=Path(static_root)
            )
            if route_error is not None:
                errors.append(f"home moment {relative_path} {route_error}")

    try:
        present = {path.relative_to(side_root).as_posix() for path in side_root.rglob("*.mp3")}
    except OSError:
        present = set()
    if present != declared:
        errors.append("home moment inventory contains missing or unlisted audio")

    if not day_one_keys:
        errors.append(
            f"home moment pack has no {HOME_MOMENT_DAY_ONE!r} entry: Step 3 would demonstrate only gated capability"
        )

    if total_bytes > HOME_MOMENT_MAX_BYTES:
        errors.append(f"home moment bundle is {total_bytes} bytes; maximum is {HOME_MOMENT_MAX_BYTES} bytes (2 MiB)")
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

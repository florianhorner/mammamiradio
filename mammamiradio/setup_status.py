"""First-run onboarding and setup status classification."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
from pathlib import Path

from mammamiradio.config import StationConfig
from mammamiradio.models import StationState
from mammamiradio.playlist import _extract_playlist_id

RUN_MODES = [
    {
        "id": "ha_addon",
        "label": "Home Assistant Add-on",
        "description": "Best when you want the radio inside Home Assistant with ingress and Spotify Connect.",
        "surface": "Add-on Configuration",
    },
    {
        "id": "docker",
        "label": "Docker",
        "description": "Runs from docker compose with env vars and one local port.",
        "surface": ".env for docker compose",
    },
    {
        "id": "macos",
        "label": "Mac App",
        "description": "One-click macOS setup with the bundled app launcher and local dashboard.",
        "surface": "Mac app setup files",
    },
    {
        "id": "local",
        "label": "Local Dev",
        "description": "Direct Python run with editable config and full local control.",
        "surface": ".env and radio.toml",
    },
]

GO_LIBRESPOT_CANDIDATES = (
    "/opt/homebrew/bin/go-librespot",
    "/usr/local/bin/go-librespot",
    "/usr/bin/go-librespot",
    "/bin/go-librespot",
)


def resolve_go_librespot_bin(configured: str) -> str | None:
    """Find a working go-librespot binary even when PATH is sparse."""
    if os.path.isabs(configured) and os.access(configured, os.X_OK):
        return configured

    discovered = shutil.which(configured)
    if discovered:
        return discovered

    for candidate in GO_LIBRESPOT_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate

    return None


def detect_run_mode(config: StationConfig) -> dict:
    """Guess the current deployment path so onboarding can set context."""
    if config.is_addon:
        detected = "ha_addon"
    elif Path("/.dockerenv").exists():
        detected = "docker"
    elif platform.system() == "Darwin":
        detected = "macos"
    else:
        detected = "local"

    modes: list[dict[str, object]] = []
    for mode in RUN_MODES:
        entry: dict[str, object] = dict(mode)
        entry["detected"] = mode["id"] == detected
        modes.append(entry)

    return {
        "detected": detected,
        "modes": modes,
    }


def _playlist_is_demo(state: StationState) -> bool:
    """Check if the loaded playlist is the built-in demo set.

    Matches the ``demo`` prefix convention from ``playlist.py:DEMO_TRACKS``.
    """
    if not state.playlist:
        return True
    return all(track.spotify_id.startswith("demo") for track in state.playlist[:5])


def _sanitize_error(exc: Exception) -> str:
    text = str(exc).strip().replace("\n", " ")
    if not text:
        return exc.__class__.__name__
    return text[:180]


def _probe_playlist_url(config: StationConfig) -> tuple[str, str]:
    """Check whether the configured playlist URL is actually reachable."""
    if not config.spotify_client_id or not config.spotify_client_secret:
        return "missing", "Spotify credentials are missing, so playlist checks are skipped."
    if not config.playlist.spotify_url:
        if config.is_addon:
            return "missing", "Add-on mode needs a public playlist URL for a deterministic first run."
        return (
            "degraded",
            "No playlist URL is set. Local runs may try liked songs later, but first run is less predictable.",
        )

    playlist_id = _extract_playlist_id(config.playlist.spotify_url)
    if not playlist_id:
        return "invalid", "The playlist URL does not look like a valid Spotify playlist link."

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
    except ImportError:
        return "missing", "The spotipy library is not installed, so playlist probing is unavailable."

    try:
        sp = None
        if not config.is_addon:
            oauth = SpotifyOAuth(
                client_id=config.spotify_client_id,
                client_secret=config.spotify_client_secret,
                redirect_uri="http://127.0.0.1:8888/callback",
                scope="user-modify-playback-state user-read-playback-state user-library-read playlist-read-private",
                cache_path=".spotify_token_cache",
                open_browser=False,
            )
            if oauth.cache_handler.get_cached_token():
                sp = spotipy.Spotify(auth_manager=oauth)

        if sp is None:
            auth = SpotifyClientCredentials(
                client_id=config.spotify_client_id,
                client_secret=config.spotify_client_secret,
            )
            sp = spotipy.Spotify(auth_manager=auth)

        results = sp.playlist_tracks(playlist_id, limit=1)
        items = results.get("items", [])
        if items and items[0].get("track", {}).get("id"):
            return "configured", "Spotify accepted the playlist URL and returned at least one track."
        return "invalid", "Spotify reached the playlist, but no playable tracks came back."
    except Exception as exc:
        return "invalid", f"Spotify rejected the playlist check: {_sanitize_error(exc)}"


def classify_station_mode(
    config: StationConfig,
    state: StationState,
    *,
    demo_playlist: bool | None = None,
    go_bin: str | None = None,
) -> dict:
    """Collapse many setup details into one operator-friendly runtime mode."""
    if demo_playlist is None:
        demo_playlist = _playlist_is_demo(state)
    has_spotify_creds = bool(config.spotify_client_id and config.spotify_client_secret)
    has_playlist_url = bool(config.playlist.spotify_url)
    if go_bin is None:
        go_bin = resolve_go_librespot_bin(config.audio.go_librespot_bin)

    if demo_playlist and not has_spotify_creds and not has_playlist_url:
        return {
            "id": "demo",
            "label": "Demo Mode",
            "summary": "The station is intentionally running built-in demo tracks.",
            "detail": (
                "Add Spotify credentials and a playlist when you want real music instead of the bundled demo set."
            ),
        }

    if not demo_playlist and state.spotify_connected and go_bin:
        return {
            "id": "real_spotify",
            "label": "Real Spotify Mode",
            "summary": "Spotify tracks are loaded and the mammamiradio device is connected.",
            "detail": "You should hear your real playlist and can fine-tune the station from the control plane.",
        }

    if demo_playlist:
        return {
            "id": "degraded",
            "label": "Degraded",
            "summary": "The station fell back to demo tracks because the Spotify path is not fully working yet.",
            "detail": "Fix the missing Spotify setup items below, then re-run preflight.",
        }

    return {
        "id": "degraded",
        "label": "Degraded",
        "summary": "Real tracks are loaded, but the live Spotify playback path is not fully ready.",
        "detail": "This usually means the Spotify Connect device is not connected yet or go-librespot is unavailable.",
    }


def addon_options_snippet(config: StationConfig) -> str:
    """Return a copy-friendly JSON block for the Home Assistant add-on UI."""
    values = {
        "anthropic_api_key": "*** configured ***" if config.anthropic_api_key else "<optional>",
        "spotify_client_id": "*** configured ***" if config.spotify_client_id else "<paste client id>",
        "spotify_client_secret": "*** configured ***" if config.spotify_client_secret else "<paste client secret>",
        "playlist_spotify_url": config.playlist.spotify_url or "<paste public playlist url>",
    }
    return json.dumps(values, indent=2)


def build_setup_status(config: StationConfig, state: StationState, *, probe: bool = False) -> dict:
    """Produce the full onboarding payload used by the dashboard gate."""
    mode = detect_run_mode(config)
    go_bin = resolve_go_librespot_bin(config.audio.go_librespot_bin)
    ffmpeg_bin = shutil.which("ffmpeg")
    demo_playlist = _playlist_is_demo(state)
    if probe:
        playlist_probe_status, playlist_probe_detail = _probe_playlist_url(config)
    else:
        playlist_probe_status, playlist_probe_detail = "skipped", "Skipped — click Re-check to probe Spotify."
    station_mode = classify_station_mode(config, state, demo_playlist=demo_playlist, go_bin=go_bin)

    essentials = [
        {
            "key": "spotify",
            "label": "Spotify",
            "required": True,
            "required_label": "Required for real Spotify radio",
            "status": "configured" if config.spotify_client_id and config.spotify_client_secret else "missing",
            "summary": (
                "Spotify credentials are present."
                if config.spotify_client_id and config.spotify_client_secret
                else "No Spotify credentials found. The app will use demo tracks instead."
            ),
            "next_action": "Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET, then re-check.",
            "skip_outcome": "If you skip this, mammamiradio stays in demo mode.",
            "where": {
                "ha_addon": "Add-on Configuration",
                "docker": ".env used by docker compose",
                "macos": "the generated .env file behind the Mac launcher",
                "local": ".env in the project root",
            },
        },
        {
            "key": "playlist",
            "label": "Playlist Source",
            "required": True,
            "required_label": "Required for a predictable first station",
            "status": playlist_probe_status,
            "summary": playlist_probe_detail,
            "next_action": (
                "Paste a public Spotify playlist URL into PLAYLIST_SPOTIFY_URL."
                if not config.playlist.spotify_url
                else "Make sure the playlist is public or owned by the authenticated Spotify account."
            ),
            "skip_outcome": (
                "Without this, first run may fall back to demo tracks."
                if config.is_addon
                else "Without this, local runs may try liked songs later or fall back to demo."
            ),
            "where": {
                "ha_addon": "Add-on Configuration, field: playlist_spotify_url",
                "docker": ".env entry: PLAYLIST_SPOTIFY_URL",
                "macos": "the launcher-generated .env file",
                "local": ".env or radio.toml",
            },
        },
        {
            "key": "anthropic",
            "label": "Anthropic",
            "required": False,
            "required_label": "Optional for AI banter and ads",
            "status": "configured" if config.anthropic_api_key else "missing",
            "summary": (
                "Claude generation is available for banter and ads."
                if config.anthropic_api_key
                else "No Anthropic key found. The station still works, but banter and ads use fallback copy."
            ),
            "next_action": "Add ANTHROPIC_API_KEY if you want the full AI radio experience.",
            "skip_outcome": "If you skip this, the station still runs with simpler stock lines.",
            "where": {
                "ha_addon": "Add-on Configuration",
                "docker": ".env used by docker compose",
                "macos": "the generated .env file behind the Mac launcher",
                "local": ".env in the project root",
            },
        },
        {
            "key": "homeassistant",
            "label": "Home Assistant",
            "required": False,
            "required_label": "Optional ambient context",
            "status": (
                "configured"
                if config.homeassistant.enabled and config.ha_token
                else "skipped"
                if not config.homeassistant.enabled
                else "missing"
            ),
            "summary": (
                "Home Assistant context is available for references in banter."
                if config.homeassistant.enabled and config.ha_token
                else "Home Assistant integration is off."
                if not config.homeassistant.enabled
                else "Home Assistant is enabled, but no token is available."
            ),
            "next_action": (
                "Nothing to do. Add-on mode wires this up automatically."
                if config.is_addon
                else "Enable Home Assistant in radio.toml and provide HA_TOKEN if you want live home context."
            ),
            "skip_outcome": "If you skip this, the hosts just stop referencing your home state.",
            "where": {
                "ha_addon": "Automatic via Supervisor",
                "docker": ".env plus [homeassistant] in radio.toml",
                "macos": ".env plus [homeassistant] in radio.toml",
                "local": ".env plus [homeassistant] in radio.toml",
            },
        },
    ]

    preflight_checks = [
        {
            "key": "ffmpeg",
            "label": "ffmpeg",
            "status": "ok" if ffmpeg_bin else "fail",
            "detail": (
                "ffmpeg is available for normalization, ads, and format conversion."
                if ffmpeg_bin
                else "ffmpeg is missing from PATH, so audio rendering will fail."
            ),
        },
        {
            "key": "go_librespot",
            "label": "go-librespot",
            "status": "ok" if go_bin else "fail",
            "detail": (
                f"go-librespot is available at {go_bin}."
                if go_bin
                else "go-librespot is missing, so Spotify Connect playback cannot come up."
            ),
        },
        {
            "key": "playlist_probe",
            "label": "Playlist probe",
            "status": (
                "ok"
                if playlist_probe_status == "configured"
                else "warn"
                if playlist_probe_status in {"degraded", "missing"}
                else "fail"
            ),
            "detail": playlist_probe_detail,
        },
        {
            "key": "playlist_loaded",
            "label": "Current loaded tracks",
            "status": "warn" if demo_playlist else "ok",
            "detail": (
                "The station currently has demo tracks loaded."
                if demo_playlist
                else "The station currently has non-demo Spotify tracks loaded."
            ),
        },
        {
            "key": "spotify_connect",
            "label": "Spotify Connect live",
            "status": "ok" if state.spotify_connected else "warn",
            "detail": (
                "The mammamiradio device is connected in Spotify."
                if state.spotify_connected
                else "Open Spotify, tap the speaker icon, and select the mammamiradio device."
            ),
        },
    ]

    launch = {
        "headline": station_mode["summary"],
        "detail": station_mode["detail"],
        "cta": (
            "Start Station"
            if station_mode["id"] == "real_spotify"
            else "Start Demo Station"
            if station_mode["id"] == "demo"
            else "Start Station Anyway"
        ),
        "post_launch": (
            "You will land in the control plane and the player will try to start immediately."
            if station_mode["id"] == "real_spotify"
            else "You will land in the control plane with a clear mode banner so nothing is ambiguous."
        ),
    }

    onboarding_required = station_mode["id"] != "real_spotify"

    signature_data = {
        "mode": mode["detected"],
        "station_mode": station_mode["id"],
        "essentials": [(item["key"], item["status"]) for item in essentials],
        "checks": [(item["key"], item["status"]) for item in preflight_checks],
    }
    signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest()[:12]

    return {
        "detected_mode": mode["detected"],
        "available_modes": mode["modes"],
        "station_mode": station_mode,
        "onboarding_required": onboarding_required,
        "essentials": essentials,
        "preflight_checks": preflight_checks,
        "launch": launch,
        "addon_options_snippet": addon_options_snippet(config) if config.is_addon else "",
        "signature": signature,
    }

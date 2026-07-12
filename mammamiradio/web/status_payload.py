"""Status payload serializers shared by admin and listener HTTP surfaces."""

from __future__ import annotations

import asyncio
import copy
import math
import os
import time
from pathlib import Path
from typing import Any

from mammamiradio.core.models import Heading, PlaylistSource, StationState, Track
from mammamiradio.playlist.playlist import normalized_track_key
from mammamiradio.playlist.preferences import preference_score
from mammamiradio.web.assets import _ASSETS_DIR


def _page_bounds(offset: int, limit: int, *, default_limit: int, max_limit: int) -> tuple[int, int]:
    """Clamp client pagination params to bounded, non-negative integers."""
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default_limit
    return max(0, offset), max(1, min(limit, max_limit))


def _has_any_mp3(path: Path) -> bool:
    """Return True when a directory contains at least one MP3 file."""
    if not path.exists() or not path.is_dir():
        return False
    return any(path.glob("*.mp3"))


_golden_path_cache: dict | None = None
_golden_path_cache_ts: float = 0.0
_golden_path_cache_key: tuple | None = None
_GOLDEN_PATH_TTL = 10.0  # seconds — music sources change rarely

_cache_size_mb_val: float = 0.0
_cache_size_mb_ts: float = 0.0
_CACHE_SIZE_TTL = 30.0  # seconds — stat()-ing every MP3 is expensive on Pi


def _cached_cache_size_mb(cache_dir: Path) -> float:
    """Return total MP3 cache size in MB, recomputed at most every 30s."""
    global _cache_size_mb_val, _cache_size_mb_ts
    now = time.time()
    if (now - _cache_size_mb_ts) < _CACHE_SIZE_TTL:
        return _cache_size_mb_val
    _cache_size_mb_val = round(
        sum(f.stat().st_size for f in cache_dir.glob("*.mp3") if f.is_file()) / (1024 * 1024),
        1,
    )
    _cache_size_mb_ts = now
    return _cache_size_mb_val


def _golden_path_status(config, state, *, force_refresh: bool = False) -> dict:
    """Build a single, explicit music onboarding status for UI surfaces."""
    global _golden_path_cache, _golden_path_cache_key, _golden_path_cache_ts
    now = time.time()
    env_allow_ytdlp = os.getenv("MAMMAMIRADIO_ALLOW_YTDLP", "false").lower() in ("true", "1", "yes")
    allow_ytdlp = bool(getattr(config, "allow_ytdlp", env_allow_ytdlp))
    cache_key = (
        allow_ytdlp,
        getattr(state, "playlist_revision", 0),
        getattr(state, "source_revision", 0),
        bool(getattr(state, "session_stopped", False)),
    )
    if (
        not force_refresh
        and _golden_path_cache is not None
        and _golden_path_cache_key == cache_key
        and (now - _golden_path_cache_ts) < _GOLDEN_PATH_TTL
    ):
        return _golden_path_cache

    has_demo_assets = _has_any_mp3(_ASSETS_DIR / "demo" / "music")
    has_local_music = _has_any_mp3(Path("music"))

    sources: list[str] = []
    if has_demo_assets:
        sources.append("bundled demo tracks")
    if has_local_music:
        sources.append("local music/*.mp3 files")
    playlist = getattr(state, "playlist", None)
    if isinstance(playlist, list | tuple) and playlist:
        source = getattr(state, "playlist_source", None)
        sources.append(getattr(source, "label", "") or "loaded playlist")
    if allow_ytdlp:
        sources.append("yt-dlp downloads")

    shared = {
        "fallback_sources": sources,
        "silent_music_fallback": not sources,
    }

    if sources:
        source_label = ", ".join(sources)
        has_llm = bool(config.anthropic_api_key or config.openai_api_key)
        result = {
            "stage": "music_available",
            "blocking": False,
            "headline": f"Music via {source_label}.",
            "detail": (
                f"Playing music from: {source_label}."
                + ("" if has_llm else " Add an Anthropic API key for AI-generated banter.")
            ),
            "steps": [],
            **shared,
        }
        _golden_path_cache = result
        _golden_path_cache_key = cache_key
        _golden_path_cache_ts = now
        return result

    result = {
        "stage": "needs_music_source",
        "blocking": True,
        "headline": "No music source configured.",
        "detail": "Set MAMMAMIRADIO_ALLOW_YTDLP=true or add MP3 files to music/.",
        "steps": [
            "Set MAMMAMIRADIO_ALLOW_YTDLP=true for chart music, or",
            "Place MP3 files in the music/ directory.",
        ],
        **shared,
    }
    _golden_path_cache = result
    _golden_path_cache_key = cache_key
    _golden_path_cache_ts = now
    return result


_HA_REFRESH_RESULTS = frozenset({"success", "failed", "background_timeout", "stale"})


def _finite_timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _nonnegative_milliseconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        return None
    return round(value)


def _positive_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _ha_mailbox_state(state: StationState) -> tuple[bool, bool, str | None, int | None, bool | None]:
    """Read producer mailbox status without letting serialization mutate it."""
    mailbox = getattr(state, "ha_context_refresh_mailbox", None)
    reader = getattr(mailbox, "read_refresh_mailbox_status", None)
    if callable(reader):
        try:
            mailbox_status = reader()
        except Exception:  # pragma: no cover - diagnostics must stay fail-soft
            mailbox_status = None
        if isinstance(mailbox_status, dict):
            in_flight = bool(mailbox_status.get("in_flight", False))
            adoption_pending = bool(mailbox_status.get("adoption_pending", False))
            result = mailbox_status.get("last_result")
            duration_ms = mailbox_status.get("last_result_duration_ms")
            used_background = mailbox_status.get("last_result_used_background")
            return (
                in_flight,
                adoption_pending,
                result if isinstance(result, str) else None,
                _nonnegative_milliseconds(duration_ms),
                bool(used_background) if used_background is not None else None,
            )

    task = getattr(mailbox, "in_flight_task", None)
    if task is None:
        return bool(getattr(state, "ha_context_refresh_in_flight", False)), False, None, None, None
    try:
        done = bool(task.done())
    except Exception:  # pragma: no cover - diagnostics must stay fail-soft
        return bool(getattr(state, "ha_context_refresh_in_flight", False)), False, None, None, None
    if not done:
        return True, False, None, None, None
    # Compatibility fallback for an embedding that exposes only a Task rather
    # than the coordinator's richer reader. A task that failed must never be
    # presented as an update ready for adoption.
    try:
        outcome = task.result()
    except TimeoutError:
        return False, False, "background_timeout", None, None
    except (asyncio.CancelledError, Exception):
        return False, False, "failed", None, None
    kind = getattr(outcome, "kind", None)
    duration_seconds = getattr(outcome, "duration_seconds", None)
    duration = (
        _nonnegative_milliseconds(duration_seconds * 1000)
        if isinstance(duration_seconds, int | float) and not isinstance(duration_seconds, bool)
        else None
    )
    if kind == "fresh":
        return False, True, "success", duration, None
    return False, False, "failed", duration, None


def _ha_refresh_payload(state: StationState) -> dict[str, object]:
    """Serialize coarse HA refresh telemetry for the authenticated admin surface."""
    last_success_at = _finite_timestamp(state.ha_context_last_updated)
    last_attempt_at = _finite_timestamp(getattr(state, "ha_context_refresh_last_attempt_at", 0.0))
    age = max(0.0, time.time() - last_success_at) if last_success_at is not None else None
    age_seconds = int(age) if age is not None else None
    stale_after_seconds = _positive_seconds(getattr(state, "ha_context_refresh_stale_after_seconds", 0.0))
    stale_by_age = bool(age is not None and stale_after_seconds is not None and age >= stale_after_seconds)
    in_flight, adoption_pending, mailbox_result, mailbox_duration_ms, mailbox_used_background = _ha_mailbox_state(state)
    raw_result = getattr(state, "ha_context_refresh_last_result", "")
    state_last_result = raw_result if isinstance(raw_result, str) and raw_result in _HA_REFRESH_RESULTS else None
    mailbox_last_result = mailbox_result if mailbox_result in _HA_REFRESH_RESULTS else None
    last_result = mailbox_last_result or state_last_result
    last_result_duration_ms = (
        mailbox_duration_ms
        if mailbox_last_result is not None
        else _nonnegative_milliseconds(getattr(state, "ha_context_refresh_last_result_duration_ms", None))
    )
    last_result_used_background = (
        mailbox_used_background
        if mailbox_last_result is not None and mailbox_used_background is not None
        else bool(getattr(state, "ha_context_refresh_last_result_used_background", False))
    )

    if last_success_at is None:
        freshness = "unavailable"
    elif stale_by_age or bool(getattr(state, "ha_context_refresh_stale", False)):
        freshness = "stale"
    else:
        freshness = "fresh"

    return {
        "freshness": freshness,
        "in_flight": in_flight,
        "adoption_pending": adoption_pending,
        "last_success_at": last_success_at,
        "age_seconds": age_seconds,
        "last_attempt_at": last_attempt_at,
        "active_foreground_timed_out": bool(
            in_flight and getattr(state, "ha_context_refresh_active_foreground_timed_out", False)
        ),
        "last_result": last_result,
        "last_result_duration_ms": last_result_duration_ms,
        "last_result_used_background": last_result_used_background,
    }


def _ha_details_payload(state: StationState) -> dict | None:
    refresh = _ha_refresh_payload(state)
    director_status: dict | None = None
    director = getattr(state, "home_context_director", None)
    if director is not None:
        try:
            candidate = director.admin_status()
            director_status = candidate if isinstance(candidate, dict) else None
        except Exception:
            director_status = None
    has_ha_observability = bool(
        state.ha_context
        or state.ha_scored_entities
        or state.ha_denylist_hits
        or state.ha_ritual_public_families
        or state.ha_ritual_matches
        or state.ha_ritual_recipe_audit
        or refresh["in_flight"]
        or refresh["last_success_at"]
        or refresh["last_attempt_at"]
        or refresh["last_result"]
        or bool(getattr(state, "ha_context_refresh_configured", False))
        or director_status
    )
    if not has_ha_observability:
        return None
    payload: dict[str, object] = {
        "mood": state.ha_home_mood or None,
        "weather_arc": state.ha_weather_arc or None,
        "events_summary": state.ha_events_summary or None,
        "pending_directive": state.ha_pending_directive or None,
        "recent_event_count": state.ha_recent_event_count,
        "last_event_label": state.ha_last_event_label or None,
        "mood_en": state.ha_home_mood_en or None,
        "weather_arc_en": state.ha_weather_arc_en or None,
        "events_summary_en": state.ha_events_summary_en or None,
        "last_event_label_en": state.ha_last_event_label_en or None,
        "scored_entities": state.ha_scored_entities[:12],
        "denylist_hits": dict(state.ha_denylist_hits),
        "catalog_hit_rate": state.ha_catalog_hit_rate,
        "label_stats": dict(state.ha_label_stats),
        "registry_source": state.ha_registry_source or None,
        "context_char_count": state.ha_context_char_count,
        "context_entity_count": state.ha_context_entity_count,
        # Legacy source-snapshot timestamp. `refresh.last_success_at` carries
        # the same value for the new admin contract.
        "context_last_updated": refresh["last_success_at"],
        "refresh": refresh,
        "first_home_context_moment_fired": state.ha_first_home_context_moment_fired,
    }
    if state.ha_ritual_public_families or state.ha_ritual_matches or state.ha_ritual_recipe_audit:
        payload["rituals"] = {
            "public_families": list(state.ha_ritual_public_families),
            "matches": copy.deepcopy(state.ha_ritual_matches[:8]),
            "audit": copy.deepcopy(state.ha_ritual_recipe_audit[:16]),
        }
    if director_status is not None:
        payload["home_context_director"] = director_status
    return payload


def _serialize_source(source: PlaylistSource | None) -> dict | None:
    if not source:
        return None
    return {
        "kind": source.kind,
        "source_id": source.source_id,
        "url": source.url,
        "label": source.label,
        "track_count": source.track_count,
        "selected_at": source.selected_at,
    }


def _heading_playlist_track_count(state: StationState, heading_id: str) -> int:
    if not heading_id:
        return 0
    return sum(1 for track in state.playlist if track.heading_id == heading_id)


def _serialize_heading(heading: Heading | None, state: StationState | None = None) -> dict:
    if heading is None:
        return {"active": False, "id": "", "seed": "", "label": "", "phase": "auto"}
    tagged = _heading_playlist_track_count(state, heading.id) if state is not None else 0
    target_count = len(heading.targets) or tagged or max(0, int(heading.selection_budget or 0))
    phase = heading.phase if heading.phase in {"hunting", "steering", "complete"} else "steering"
    if state is not None and phase != "complete":
        phase = "hunting" if tagged == 0 and len(heading.targets) > 0 else "steering"
    resolving = phase == "hunting"
    data = {
        "active": True,
        "id": heading.id,
        "seed": heading.seed,
        "label": heading.label,
        "set_at": heading.set_at,
        "set_by": heading.set_by,
        "phase": phase,
        "selection_budget": heading.selection_budget,
        "selection_spent": heading.selection_spent,
        "selection_remaining": max(0, heading.selection_budget - heading.selection_spent),
        "target_count": target_count,
    }
    # When state is available, report how many rotation tracks actually carry this
    # course yet. A text direction restored at boot (or mid-resolve) has targets
    # but zero tagged tracks until its background search/download lands — surface
    # that as `resolving` so the admin banner can say "finding songs…" instead of
    # claiming the course is already steering (honesty; leadership #5).
    if state is not None:
        data["tagged_count"] = tagged
        data["resolving"] = resolving
    return data


def _serialize_brand(brand) -> dict:
    """Serialize listener/admin brand config through one shared shape."""
    return {
        "station_name": brand.station_name,
        "frequency": brand.frequency,
        "city": brand.city,
        "founded": brand.founded,
        "tagline": brand.tagline,
        "about": brand.about,
        "opengraph_subtitle": brand.opengraph_subtitle,
        "hosts": [
            {"engine_host": h.engine_host, "display_name": h.display_name, "description": h.description}
            for h in brand.hosts
        ],
        "theme": {
            "primary_color": brand.theme.primary_color,
            "accent_color": brand.theme.accent_color,
            "background_color": brand.theme.background_color,
            "display_font": brand.theme.display_font,
            "body_font": brand.theme.body_font,
            "mono_font": brand.theme.mono_font,
        },
    }


def _serialize_identity(config) -> dict:
    """Serialize the resolved station identity through one additive block."""
    identity = getattr(config, "identity", None)
    station_name = (
        getattr(identity, "station_name", "") or getattr(config, "display_station_name", "") or "Mamma Mi Radio"
    )
    generated = getattr(identity, "generated", {}) or {}
    return {
        "station_name": station_name,
        "source": getattr(identity, "source", "unknown") if identity is not None else "unknown",
        "custom_copy_preserved": bool(getattr(identity, "custom_copy_preserved", False)),
        "preview": {
            "heard_on_air": generated.get("spoken_ident") or station_name,
            "seen_by_listeners": generated.get("listener_title") or station_name,
            "seen_in_home_assistant": generated.get("home_assistant_name") or station_name,
        },
    }


def _track_preference_score(track: Track, preferences: object) -> int:
    return preference_score(preferences, normalized_track_key(track))


def _serialize_track(track: Track, *, preferences: object | None = None) -> dict:
    payload = {
        "title": track.title,
        "artist": track.artist,
        "display": track.display,
        "spotify_id": track.spotify_id,
        "album_art": track.album_art,
        "source": track.source,
        "year": track.year,
        "youtube_id": track.youtube_id,
        "duration_ms": track.duration_ms,
        "heading_id": track.heading_id,
    }
    if preferences is not None:
        payload["preference"] = _track_preference_score(track, preferences)
    return payload


def _paginated_tracks(
    tracks: list[Track],
    offset: int,
    limit: int,
    *,
    revision: int | None = None,
    preferences: object | None = None,
) -> dict[str, Any]:
    total = len(tracks)
    page = tracks[offset : offset + limit]
    payload: dict[str, Any] = {
        "tracks": [_serialize_track(track, preferences=preferences) for track in page],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < total,
    }
    if revision is not None:
        payload["revision"] = revision
    return payload


def _duration_sec_from_payload(payload: dict | None) -> float | None:
    if not payload:
        return None
    duration = payload.get("duration_sec")
    if isinstance(duration, int | float) and duration > 0:
        return float(duration)
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    duration_ms = metadata.get("duration_ms")
    if isinstance(duration_ms, int | float) and duration_ms > 0:
        return float(duration_ms) / 1000.0
    duration_s = metadata.get("duration_s")
    if isinstance(duration_s, int | float) and duration_s > 0:
        return float(duration_s)
    return None


_INTERNAL_SEGMENT_METADATA_KEYS = frozenset(
    {
        # Home Context Director bookkeeping is intentionally internal.  The
        # selected fact is an opaque prompt contract, not listener-facing data.
        "home_fact_id",
        "home_fact_topic",
        "home_fact_topic_key",
        "home_fact_fingerprint",
        "home_fact_entity_id",
        "home_fact_policy_revision",
        "memory_extraction",
        "ritual_recipe_match",
        "ritual_recipe_matches",
        "ritual_recipe_audit",
        "ritual_directive",
        "ritual_moment_id",
        "gag_moment_id",
        "transition_track_ref",
    }
)


def _public_segment_metadata(metadata: object) -> dict:
    """Copy segment metadata for public/shared status payloads."""
    if not isinstance(metadata, dict):
        return {}

    def _without_internal(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: _without_internal(child)
                for key, child in value.items()
                if key not in _INTERNAL_SEGMENT_METADATA_KEYS
            }
        if isinstance(value, list):
            return [_without_internal(child) for child in value]
        if isinstance(value, tuple):
            return tuple(_without_internal(child) for child in value)
        return copy.deepcopy(value)

    public = _without_internal(metadata)
    return public if isinstance(public, dict) else {}


def _public_now_streaming_payload(now_streaming: dict | None) -> dict:
    if not now_streaming:
        return {}
    payload = copy.deepcopy(now_streaming)
    payload["metadata"] = _public_segment_metadata(payload.get("metadata"))
    return payload


def _status_now_playback(now_streaming: dict, now_ts: float) -> dict:
    duration_sec = _duration_sec_from_payload(now_streaming)
    public_now_streaming = _public_now_streaming_payload(now_streaming)
    if not now_streaming:
        return {
            "now_streaming": public_now_streaming,
            "current_progress_sec": None,
            "current_duration_sec": None,
        }
    started = now_streaming.get("started")
    progress_sec = max(0.0, now_ts - started) if isinstance(started, int | float) and started > 0 else None
    return {
        "now_streaming": public_now_streaming,
        "current_progress_sec": round(progress_sec, 1) if progress_sec is not None else None,
        "current_duration_sec": round(duration_sec, 1) if duration_sec is not None else None,
    }


def _serialize_stream_log_entry(entry) -> dict:
    payload = {
        "type": entry.type,
        "label": entry.label,
        "timestamp": entry.timestamp,
        "metadata": _public_segment_metadata(entry.metadata),
    }
    duration_sec = float(getattr(entry, "duration_sec", 0.0) or 0.0)
    if duration_sec <= 0:
        duration_sec = _duration_sec_from_payload({"metadata": entry.metadata}) or 0.0
    if duration_sec > 0:
        payload["duration_sec"] = duration_sec
        payload["duration_ms"] = round(duration_sec * 1000)
    return payload

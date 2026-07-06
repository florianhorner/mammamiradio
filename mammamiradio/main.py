"""FastAPI application entrypoint for the mammamiradio station."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI

from mammamiradio.core.config import DEFAULT_STATION_NAME, load_config
from mammamiradio.core.models import PlaylistSource, StationState
from mammamiradio.core.sync import init_db
from mammamiradio.home.entity_policy import muted_entity_ids
from mammamiradio.home.evening_memory import EveningLedger
from mammamiradio.hosts.persona import PersonaStore
from mammamiradio.hosts.verbal_gag_ledger import VerbalGagLedger
from mammamiradio.integrations import router as integrations_router
from mammamiradio.playlist.blocklist import load_blocklist
from mammamiradio.playlist.direction import (
    find_existing_direction_tracks,
    resolve_direction_tracks,
    target_dicts_to_targets,
)
from mammamiradio.playlist.downloader import evict_cache_lru, prune_stale_tmp_files, purge_suspect_cache_files
from mammamiradio.playlist.playlist import (
    DEMO_TRACKS,
    PERSISTED_HEADING_FILENAME,
    fetch_startup_playlist,
    filter_blocklisted,
    load_explicit_source,
    normalized_track_key,
    read_persisted_heading,
    read_persisted_source,
    write_persisted_heading,
)
from mammamiradio.playlist.preferences import load_preferences
from mammamiradio.release_campaign import ReleaseBeatManifest, ReleaseCampaign, ReleaseCampaignLedger
from mammamiradio.restart_handoff import admit_restart_handoff_entries, prune_stale_handoff_tmp_files
from mammamiradio.scheduling.producer import prewarm_first_segment, run_producer
from mammamiradio.web.listener_requests import router as listener_requests_router
from mammamiradio.web.streamer import (
    CLIP_MAX_SEGMENT_SECONDS,
    LiveStreamHub,
    _clear_active_heading,
    _download_direction_track,
    _heading_selection_budget,
    _register_background_task,
    _session_stopped_flag,
    router,
    run_playback_loop,
)

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


def _configure_http_logging() -> None:
    level_name = os.getenv("MAMMAMIRADIO_HTTP_LOG_LEVEL", "WARNING").strip().upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.WARNING)
    logging.getLogger("httpx").setLevel(level)
    logging.getLogger("httpcore").setLevel(level)


_configure_http_logging()
logger = logging.getLogger("mammamiradio")

_producer_task: asyncio.Task | None = None
_playback_task: asyncio.Task | None = None
_prewarm_task: asyncio.Task | None = None


def _read_persisted_chaos_mode(config) -> bool:
    """Read persisted Chaos Mode without arming a first-strike."""
    raw_env = os.getenv("MAMMAMIRADIO_CHAOS_MODE", "").strip().lower()
    if raw_env in {"true", "1", "yes", "on"}:
        return True
    if raw_env in {"false", "0", "no", "off"}:
        return False
    if getattr(config, "is_addon", False):
        options_path = Path("/data/options.json")
        try:
            options = json.loads(options_path.read_text()) if options_path.exists() else {}
        except (OSError, ValueError):
            options = {}
        if not isinstance(options, dict):
            options = {}
        if isinstance(options.get("chaos_mode_active"), bool):
            return bool(options["chaos_mode_active"])
    return False


def _clear_persisted_heading(config) -> None:
    try:
        (config.cache_dir / PERSISTED_HEADING_FILENAME).unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to clear persisted heading during startup", exc_info=True)


async def _restore_direction_targets_background(app_state, heading_id: str, raw_targets, source_revision: int) -> None:
    """Best-effort target rehydration for persisted directions after instant-audio startup."""
    try:
        targets = target_dicts_to_targets(raw_targets)
        if not targets:
            return
        resolved_tracks = await resolve_direction_tracks(targets)
        state = app_state.station_state
        resolved_tracks = filter_blocklisted(resolved_tracks, state.blocklist)
        download_tracks = []
        async with app_state.source_switch_lock:
            if state.heading is None or state.heading.id != heading_id or state.source_revision != source_revision:
                return
            existing_keys = {normalized_track_key(track) for track in state.playlist}
            seen_new: set[tuple[str, str]] = set()
            for track in resolved_tracks:
                key = normalized_track_key(track)
                if key in existing_keys or key in seen_new:
                    continue
                seen_new.add(key)
                track.heading_id = heading_id
                download_tracks.append(track)

        download_tasks = []
        for track in download_tracks:
            dl_task = asyncio.create_task(_download_direction_track(track, app_state, source_revision, heading_id))
            _register_background_task(app_state, dl_task)
            download_tasks.append(dl_task)
        if download_tasks:
            await asyncio.gather(*download_tasks, return_exceptions=True)

        async with app_state.source_switch_lock:
            state = app_state.station_state
            if state.heading is None or state.heading.id != heading_id or state.source_revision != source_revision:
                return
            playable_count = sum(1 for track in state.playlist if track.heading_id == heading_id)
            if playable_count == 0:
                logger.warning("Persisted direction restored no playable tracks; returning to auto")
                _clear_active_heading(state)
                _clear_persisted_heading(app_state.config)
    except Exception:
        logger.warning("Persisted direction background restore failed", exc_info=True)


def _admit_restart_handoff(queue: asyncio.Queue, state: StationState, config) -> int:
    """Synchronously admit safe handoff music before background tasks start."""
    if state.session_stopped:
        logger.info("Restart handoff: skipped because the station is stopped")
        return 0
    admission = admit_restart_handoff_entries(
        config.cache_dir,
        blocklist=state.blocklist,
    )
    accepted = 0
    for segment in admission.to_segments(config.cache_dir):
        if queue.full():
            break
        queue_id = uuid4().hex
        segment.metadata["queue_id"] = queue_id
        queue.put_nowait(segment)
        # Protect this file from the per-enqueue spool prune while it is still
        # queued — resolved to match how _prune_unreferenced_segments compares.
        try:
            state.restart_handoff_admitted_paths.add(segment.path.resolve(strict=False))
        except OSError:
            state.restart_handoff_admitted_paths.add(segment.path)
        state.last_enqueued_type = segment.type
        state.last_music_file = segment.path
        state.queued_segments.append(
            {
                "id": queue_id,
                "type": segment.type.value,
                "label": segment.metadata.get("title", segment.type.value),
                "spotify_id": segment.metadata.get("spotify_id", ""),
                "reason": "Restored from safe restart handoff.",
                "playlist_index": segment.metadata.get("playlist_index", -1),
                "source_kind": segment.metadata.get("source_kind", ""),
                "duration_sec": round(segment.duration_sec or 0, 1),
            }
        )
        state.last_state_change_at = time.time()
        accepted += 1
    if accepted:
        logger.info("Restart handoff: admitted %d safe music segment(s)", accepted)
    elif admission.rejected:
        logger.info("Restart handoff: no segments admitted (%d rejected)", len(admission.rejected))
    return accepted


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await startup()
    yield
    await shutdown()


app = FastAPI(title=DEFAULT_STATION_NAME, lifespan=_lifespan)
app.include_router(router)
app.include_router(listener_requests_router)
app.include_router(integrations_router)


async def startup():
    """Load config, build initial state, and start producer/playback workers."""
    global _producer_task, _playback_task, _prewarm_task

    config = load_config()
    logger.info("Station: %s (%s)", config.station.name, config.station.language)

    # One integrated-LUFS target across every segment type: configure the
    # normalizer's reconciliation pass from radio.toml [audio]. Music, dialogue,
    # bedded banter and ads then all land at the same perceived level.
    from mammamiradio.audio.normalizer import configure_broadcast_chain, configure_loudness_reconcile

    configure_loudness_reconcile(
        config.audio.lufs_target,
        config.audio.ad_lufs_target,
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        bitrate=config.audio.bitrate,
    )

    # The egress FM broadcast chain — the final "transmitter" stage every aired
    # non-rescue segment passes through so the station sounds like radio.
    configure_broadcast_chain(
        config.audio.broadcast_chain,
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        bitrate=config.audio.bitrate,
    )

    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    config.cache_dir.mkdir(parents=True, exist_ok=True)

    # Prune stale temp render scratch left by a prior run (crash/restart debris)
    # so the HA add-on's /data/tmp doesn't grow unbounded across restarts.
    pruned_tmp = prune_stale_tmp_files(config.tmp_dir)
    if pruned_tmp:
        logger.info("Temp cleanup: pruned %d stale render file(s)", pruned_tmp)
    pruned_handoff_tmp = prune_stale_handoff_tmp_files(config.cache_dir)
    if pruned_handoff_tmp:
        logger.info("Restart handoff cleanup: pruned %d stale scratch file(s)", pruned_handoff_tmp)

    if config.homeassistant.enabled and config.ha_token and config.anthropic_api_key:
        logger.info("Label generation sends entity metadata (IDs, names, areas) to LLM provider anthropic")

    # Purge suspect cache files (likely failed downloads) before serving
    purged = purge_suspect_cache_files(config.cache_dir)
    if purged:
        logger.info("Cache integrity check: purged %d suspect file(s)", purged)
    norm_count = len(list(config.cache_dir.glob("norm_*.mp3")))
    logger.info("Normalization cache: %d tracks pre-normalized", norm_count)

    # Evict old cached tracks if the cache exceeds the configured size limit
    evict_cache_lru(config.cache_dir, config.max_cache_size_mb)

    # Initialize persona database and store for compounding listener memory
    db_path = config.cache_dir / "mammamiradio.db"
    init_db(db_path)
    persona_store = PersonaStore(db_path)

    # Dependency checks with install hints
    _ffmpeg_found = bool(shutil.which("ffmpeg"))
    _ytdlp_found = bool(shutil.which("yt-dlp"))
    if not _ffmpeg_found:
        logger.warning(
            "FFmpeg not found — audio generation will fail. "
            "Install: brew install ffmpeg (macOS) or apt install ffmpeg (Linux)"
        )
    if config.allow_ytdlp and not _ytdlp_found:
        logger.warning(
            "yt-dlp not found but MAMMAMIRADIO_ALLOW_YTDLP is enabled — charts will fall back to demo. "
            "Install: brew install yt-dlp (macOS) or pip install yt-dlp"
        )

    # Restore stop state so a reload/restart honours an operator-issued stop.
    # The operator's /api/resume is the correct way to clear this — a crash or
    # watchdog restart should not silently undo a deliberate stop.
    _stopped_flag = _session_stopped_flag(config)
    _session_stopped = _stopped_flag.exists()
    if _session_stopped:
        logger.info(
            "Restoring stopped session state from previous run — use /api/resume or the admin panel to start playback"
        )

    # Restore the evening running-gag ledger so a mid-evening addon restart
    # resumes the same session and gags instead of resetting them. Missing or
    # corrupt files start fresh and never block boot. Candidacy policy comes from
    # config ([home.running_gags]); empty lists keep the built-in domain default.
    # The mute policy's entity_denylist is static (config-only) and doesn't
    # purge already-persisted buckets on its own, so a mute applied in a prior
    # session (or a purge whose save_if_dirty() failed) would otherwise
    # survive a restart and still be offerable as a running gag (codex
    # adversarial review). Merge the current mute policy into the denylist
    # AND purge any matching buckets already on disk.
    _muted_at_boot = muted_entity_ids(config.cache_dir)
    evening_ledger = EveningLedger.load(
        config.cache_dir,
        domain_allowlist=config.running_gags.domain_allowlist or None,
        entity_allowlist=config.running_gags.entity_allowlist or None,
        entity_denylist=(set(config.running_gags.entity_denylist) | _muted_at_boot) or None,
    )
    for _muted_entity_id in _muted_at_boot:
        evening_ledger.purge_entity(_muted_entity_id)
    evening_ledger.save_if_dirty(config.cache_dir)

    # Verbal running-gag ledger — in-memory, session-ephemeral (a restart
    # correctly forgets verbal gags), so unlike the evening ledger it is not
    # loaded from disk.
    verbal_gag_ledger = VerbalGagLedger()
    try:
        release_campaign = ReleaseCampaign.load(config.cache_dir)
    except Exception:
        # A corrupt/unreadable manifest or ledger must never abort the boot
        # (INSTANT AUDIO) — fall back to a fully inert campaign built with no
        # file I/O of its own, so it can't raise the same way again.
        logger.warning("Failed to load release campaign state; continuing without it", exc_info=True)
        release_campaign = ReleaseCampaign(
            config.cache_dir,
            manifest=ReleaseBeatManifest.disabled(),
            ledger=ReleaseCampaignLedger.fresh(""),
        )

    persisted_source = read_persisted_source(config.cache_dir)
    logger.info("Fetching startup playlist")
    try:
        tracks, playlist_source, startup_source_error = fetch_startup_playlist(config, persisted_source)
    except Exception as e:
        logger.error("Playlist fetch crashed: %s — using demo playlist", e)
        tracks = list(DEMO_TRACKS)

        playlist_source = PlaylistSource(
            kind="demo",
            source_id="demo",
            label="Built-in modern Italian demo mix",
            track_count=len(tracks),
            selected_at=time.time(),
        )
        startup_source_error = str(e)

    # Persistent operator blocklist: a song the operator banned must never re-enter
    # the pool, including on this cold-start re-fetch (the reported "deleted songs
    # come back after restart" bug). Filter the freshly fetched pool before it ever
    # reaches the producer. Best-effort load — a missing/corrupt file bans nothing.
    blocklist = load_blocklist(config.cache_dir)
    pre_blocklist_count = len(tracks)
    tracks = filter_blocklisted(tracks, blocklist)
    if pre_blocklist_count != len(tracks):
        logger.info(
            "Blocklist: filtered %d banned track(s) from the startup pool",
            pre_blocklist_count - len(tracks),
        )
    song_preferences = load_preferences(config.cache_dir)
    logger.info("Song preferences: loaded %d preference(s)", len(song_preferences))

    persisted_heading = read_persisted_heading(config.cache_dir)
    pending_direction_targets: list[dict[str, str]] = []
    if persisted_heading is not None:
        existing_heading_tracks = []
        try:
            if persisted_heading.targets:
                heading_targets = target_dicts_to_targets(persisted_heading.targets)
                if not heading_targets:
                    raise ValueError("persisted direction has no valid targets")
                pending_direction_targets = [target.to_dict() for target in heading_targets]
                existing_heading_tracks = find_existing_direction_tracks(tracks, heading_targets)
                heading_tracks = []
            else:
                heading_tracks, _heading_source = load_explicit_source(
                    config,
                    PlaylistSource(kind="url", url=persisted_heading.seed),
                )
            heading_tracks = filter_blocklisted(heading_tracks, blocklist)
        except Exception as exc:
            logger.warning("Persisted heading restore failed; returning to auto: %s", exc)
            _clear_persisted_heading(config)
            persisted_heading = None
        else:
            if not heading_tracks and not existing_heading_tracks and not pending_direction_targets:
                logger.warning("Persisted heading restored no playable tracks; returning to auto")
                _clear_persisted_heading(config)
                persisted_heading = None
            else:
                existing_by_key = {normalized_track_key(track): track for track in tracks}
                blended_heading_tracks = []
                retagged_existing = 0
                for track in existing_heading_tracks:
                    if track.heading_id != persisted_heading.id:
                        track.heading_id = persisted_heading.id
                        retagged_existing += 1
                    existing_by_key[normalized_track_key(track)] = track
                for track in heading_tracks:
                    key = normalized_track_key(track)
                    existing_track = existing_by_key.get(key)
                    if existing_track is not None:
                        if existing_track.heading_id != persisted_heading.id:
                            existing_track.heading_id = persisted_heading.id
                            retagged_existing += 1
                        continue
                    track.heading_id = persisted_heading.id
                    existing_by_key[key] = track
                    blended_heading_tracks.append(track)
                restored_count = retagged_existing + len(blended_heading_tracks)
                if not restored_count and not pending_direction_targets:
                    logger.warning("Persisted heading restored no matching tracks; returning to auto")
                    _clear_persisted_heading(config)
                    persisted_heading = None
                else:
                    if persisted_heading.selection_budget <= 0:
                        persisted_heading.selection_budget = _heading_selection_budget(
                            restored_count or len(pending_direction_targets)
                        )
                    if restored_count:
                        persisted_heading.phase = "steering"
                        if persisted_heading.first_found_at <= 0:
                            persisted_heading.first_found_at = time.time()
                    else:
                        persisted_heading.phase = "hunting"
                    tracks = tracks + blended_heading_tracks
                    logger.info(
                        "Restored heading %s with %d fetched track(s), %d blended, %d retagged",
                        persisted_heading.label,
                        len(heading_tracks),
                        len(blended_heading_tracks),
                        retagged_existing,
                    )
    logger.info("Loaded %d tracks", len(tracks))

    state = StationState(
        playlist=tracks,
        playlist_source=playlist_source,
        startup_source_error=startup_source_error,
        heading=persisted_heading,
        heading_announced_id=persisted_heading.id
        if persisted_heading is not None and persisted_heading.announced
        else "",
        blocklist=blocklist,
        song_preferences=song_preferences,
        persona_store=persona_store,
        evening_ledger=evening_ledger,
        verbal_gag_ledger=verbal_gag_ledger,
        release_campaign=release_campaign,
        session_stopped=_session_stopped,
        chaos_mode_active=_read_persisted_chaos_mode(config),
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=config.pacing.lookahead_segments + 2)

    # Ring buffer for clip sharing ("share WTF moment"). Sized to hold the longest
    # shareable ad/banter segment (CLIP_MAX_SEGMENT_SECONDS) so a full spot can be
    # captured whole; music clips still only read the trailing 30s. Chunks are the
    # ~4 KB MP3 reads fed by the playback send loop. The max(240, …) floor keeps a
    # usable buffer when bitrate is missing or malformed.
    from collections import deque

    _clip_chunk_bytes = 4096
    try:
        _bytes_per_sec = int(config.audio.bitrate) * 1000 // 8
        _clip_maxlen = max(240, _bytes_per_sec * CLIP_MAX_SEGMENT_SECONDS // _clip_chunk_bytes)
    except (TypeError, ValueError, AttributeError):
        _clip_maxlen = 240
    app.state.clip_ring_buffer: deque[bytes] = deque(maxlen=_clip_maxlen)
    app.state.last_shareworthy_clip = None

    # Set app.state for streamer access
    app.state.queue = queue
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.stream_hub = LiveStreamHub()
    app.state.stream_hub.bind_state(state)
    app.state.station_state = state
    app.state.release_campaign = release_campaign
    app.state.config = config
    app.state.start_time = time.time()

    def _persist_heading_update(heading) -> None:
        async def _write_heading() -> None:
            # Serialize against clear_heading/source switches and re-check identity
            # under the lock: after_music fires this off the hot path, so without
            # the lock a write could land AFTER a "Back to auto" deleted heading.json
            # and resurrect the just-cleared course on the next restart.
            try:
                async with app.state.source_switch_lock:
                    current = state.heading
                    if current is None or current.id != heading.id:
                        return
                    await asyncio.to_thread(write_persisted_heading, config.cache_dir, current)
            except Exception:
                logger.warning("Failed to persist heading update", exc_info=True)

        task = asyncio.create_task(_write_heading())
        _register_background_task(app.state, task)

    state.heading_persist_callback = _persist_heading_update

    # Provenance ledger (Show Memory). Start BEFORE producer/playback so the
    # earliest segments are captured, and stop AFTER them on shutdown so final
    # rows survive. Hung off state so all three capture tiers reach it
    # (_generate_json_response, producer, on_stream_segment) without app access.
    from mammamiradio.core.ledger import ProvenanceLedger

    ledger = ProvenanceLedger(
        config.ledger_dir,
        enabled=config.ledger_enabled,
        retention_days=config.ledger_retention_days,
        queue_max=config.ledger_queue_max,
    )
    ledger.start()
    app.state.ledger = ledger
    state.ledger = ledger

    try:
        _admit_restart_handoff(queue, state, config)
    except Exception:
        # Best-effort cold-open bridge — a corrupt manifest or a TOCTOU race
        # reading a spooled file must never abort startup (INSTANT AUDIO).
        logger.warning("Restart handoff admission failed; continuing without it", exc_info=True)

    # Pre-produce music segments in the background so app startup is instant.
    # If a listener arrives before prewarm finishes, the producer's idle-resume
    # logic queues a canned clip as an immediate fallback.
    # Keep prewarm capped at 2 across environments to avoid ffmpeg pileups on
    # constrained addon hardware while still buffering enough for smooth start.
    async def _prewarm_multiple():
        total = 2
        for _ in range(total):
            await prewarm_first_segment(queue, state, config)

    _prewarm_task = asyncio.create_task(_prewarm_multiple())

    _playback_task = asyncio.create_task(run_playback_loop(app))
    _producer_task = asyncio.create_task(run_producer(queue, state, config, skip_event=app.state.skip_event))
    app.state.prewarm_task = _prewarm_task
    app.state.playback_task = _playback_task
    app.state.producer_task = _producer_task
    if state.heading is not None and pending_direction_targets:
        restore_task = asyncio.create_task(
            _restore_direction_targets_background(
                app.state, state.heading.id, pending_direction_targets, state.source_revision
            )
        )
        _register_background_task(app.state, restore_task)
    # Validate configured AI keys in the background so a bogus persisted key
    # surfaces in the admin BEFORE any banter fails. Fire-and-forget —
    # never awaited, so it can't delay first audio (Leadership Principle #2).
    if config.anthropic_api_key or config.openai_api_key:
        from mammamiradio.web.streamer import _run_provider_verdict

        app.state.provider_verdict_task = asyncio.create_task(_run_provider_verdict(app.state))
    # Startup diagnostics — first 5 seconds of logs must be actionable for debugging
    _config_file = Path("radio.toml").resolve()
    _audio_src = {"charts": "yt-dlp", "demo": "demo", "local": "local"}.get(
        (playlist_source.kind if playlist_source else ""), "unknown"
    )
    logger.info("Startup diagnostics:")
    logger.info("  config_file=%s  cache_dir=%s", _config_file, config.cache_dir)
    logger.info("  audio_source=%s  tracks=%d", _audio_src, len(tracks))
    logger.info(
        "  keys: anthropic=%s  openai=%s  ha_token=%s",
        "set" if os.getenv("ANTHROPIC_API_KEY") else "missing",
        "set" if os.getenv("OPENAI_API_KEY") else "missing",
        "set" if os.getenv("HA_TOKEN") else "missing",
    )
    logger.info(
        "  deps: ffmpeg=%s  ytdlp=%s (allowed=%s)",
        "found" if _ffmpeg_found else "MISSING",
        "found" if _ytdlp_found else "missing",
        "yes" if config.allow_ytdlp else "no",
    )
    logger.info(
        "Producer started. Stream at http://%s:%d/stream",
        config.bind_host,
        config.port,
    )


async def shutdown():
    """Stop background workers and close shared streaming resources."""
    tasks_to_cancel = []
    if _prewarm_task:
        _prewarm_task.cancel()
        tasks_to_cancel.append(_prewarm_task)
    if _producer_task:
        _producer_task.cancel()
        tasks_to_cancel.append(_producer_task)
    if _playback_task:
        _playback_task.cancel()
        tasks_to_cancel.append(_playback_task)
    # The provider-verdict probe is created outside the producer/playback set
    # (startup + credential saves); cancel it so it can't mutate station_state
    # after teardown begins — same write-after-shutdown race as the downloads.
    verdict_task = getattr(app.state, "provider_verdict_task", None)
    if verdict_task:
        verdict_task.cancel()
        tasks_to_cancel.append(verdict_task)
    # Fire-and-forget background tasks (queue-from-search / listener song
    # downloads). Cancel them too so an in-flight yt-dlp download can't write to
    # app.state after teardown begins.
    background_tasks = getattr(app.state, "background_tasks", None)
    if background_tasks:
        for _bg in list(background_tasks):
            _bg.cancel()
            tasks_to_cancel.append(_bg)
    # Same write-after-shutdown race as the downloads above: an in-flight
    # restart-handoff spool write does file I/O via asyncio.to_thread and must
    # not still be running once teardown proceeds.
    restart_handoff_tasks = getattr(getattr(app.state, "station_state", None), "_restart_handoff_tasks", None)
    if restart_handoff_tasks:
        for _rh in list(restart_handoff_tasks):
            _rh.cancel()
            tasks_to_cancel.append(_rh)
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    if hasattr(app.state, "producer_task"):
        app.state.producer_task = None
    if hasattr(app.state, "prewarm_task"):
        app.state.prewarm_task = None
    if hasattr(app.state, "playback_task"):
        app.state.playback_task = None
    if hasattr(app.state, "stream_hub"):
        app.state.stream_hub.close()
    # Stop the ledger AFTER producer/playback are cancelled so final rows drain.
    if getattr(app.state, "ledger", None) is not None:
        app.state.ledger.stop()
        app.state.ledger = None
    if getattr(app.state, "release_campaign", None) is not None:
        try:
            await asyncio.to_thread(app.state.release_campaign.save_if_dirty)
        except Exception:
            logger.warning("Failed to flush release campaign ledger during shutdown", exc_info=True)


if __name__ == "__main__":
    import uvicorn

    config = load_config()
    uvicorn.run(
        "mammamiradio.main:app",
        host=config.bind_host,
        port=config.port,
    )

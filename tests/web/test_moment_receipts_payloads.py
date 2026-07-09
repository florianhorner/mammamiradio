"""Moment Receipts on the status surfaces + streamer confirm/finalize contract.

Covers the privacy split (public generic labels vs admin full trail), the
airing/finalize lifecycle driven by the stream hooks, and the two safety
contracts: no disk write on the stream path, and a recording failure never
reaching the audio path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from mammamiradio.core.models import Segment, SegmentType, StationState
from mammamiradio.home.moment_receipts import STORE_FILENAME, MomentStore
from mammamiradio.web.streamer import _finalize_moment_receipts
from tests.web.test_streamer_routes import _make_test_app

NOW = 1_800_000_000.0


def _store_with_aired_row() -> tuple[MomentStore, str]:
    store = MomentStore()
    moment_id = store.record(
        lane="directive",
        family="morning_launch",
        public_label="Morning launch",
        entity_id="sensor.kitchen_coffee_power",
        confidence=0.8,
        now=NOW,
    )
    store.mark_airing(moment_id, now=NOW + 30)
    store.finalize(moment_id, "aired", now=NOW + 90)
    return store, moment_id


def _banter_segment(**metadata: object) -> Segment:
    return Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/fake-banter.mp3"),
        duration_sec=12.0,
        metadata={"type": "banter", "title": "Marco & Giulia", **metadata},
    )


def _enable_ha(app) -> None:
    app.state.config.homeassistant.enabled = True
    app.state.config.ha_token = "ha-token"


# --- payload surfaces -------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_status_recent_moments_generic_labels_only():
    app = _make_test_app()
    _enable_ha(app)
    store, _ = _store_with_aired_row()
    app.state.station_state.moment_store = store
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/public-status")).json()

    recent = body["ha_moments"]["recent"]
    assert len(recent) == 1
    # Listener-safe shape ONLY: no entity ids, no confidence, no families,
    # no raw ids may ever cross the unauthenticated boundary.
    assert set(recent[0]) == {"label", "ago_min", "status"}
    assert recent[0]["label"] == "Morning launch"
    assert recent[0]["status"] == "aired"


@pytest.mark.asyncio
async def test_public_status_hides_elected_and_dropped_moments():
    app = _make_test_app()
    _enable_ha(app)
    store = MomentStore()
    store.record(lane="directive", family="f", public_label="Elected only", now=NOW)
    dropped = store.record(lane="interrupt", family="f", public_label="Dropped", now=NOW)
    store.mark_dropped(dropped, "interrupt_cooldown", now=NOW)
    app.state.station_state.moment_store = store
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/public-status")).json()

    # Nothing aired yet → no receipts strip (ha_moments may be absent entirely).
    assert not (body.get("ha_moments") or {}).get("recent")


@pytest.mark.asyncio
async def test_admin_status_moments_trail_and_cross_page_consistency():
    app = _make_test_app()
    _enable_ha(app)
    store, moment_id = _store_with_aired_row()
    app.state.station_state.moment_store = store
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        public = (await client.get("/public-status")).json()
        admin = (await client.get("/status")).json()

    rows = admin["moments_admin"]
    assert len(rows) == 1
    assert rows[0]["id"] == moment_id
    assert rows[0]["entity_id"] == "sensor.kitchen_coffee_power"
    assert rows[0]["confidence"] == 0.8
    assert rows[0]["status"] == "aired"
    # The admin-only trail never appears on the public surface...
    assert "moments_admin" not in public
    # ...and the shared field is bytes-identical across pages (cross-page invariant).
    assert admin["ha_moments"]["recent"] == public["ha_moments"]["recent"]


@pytest.mark.asyncio
async def test_admin_status_moments_requires_admin_auth():
    app = _make_test_app(admin_password="segreto")
    store, _ = _store_with_aired_row()
    app.state.station_state.moment_store = store
    # Non-loopback client → auth ladder applies.
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_public_status_survives_store_failure():
    app = _make_test_app()
    _enable_ha(app)
    store, _ = _store_with_aired_row()
    app.state.station_state.moment_store = store
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch.object(MomentStore, "to_public_rows", side_effect=RuntimeError("boom")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/public-status")
    assert resp.status_code == 200
    assert not (resp.json().get("ha_moments") or {}).get("recent")


@pytest.mark.asyncio
async def test_public_status_hides_persisted_receipts_when_ha_is_disabled():
    app = _make_test_app()
    store, _ = _store_with_aired_row()
    app.state.station_state.moment_store = store
    assert app.state.config.homeassistant.enabled is False
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/public-status")).json()

    assert body["capabilities"]["ha"] is False
    assert not (body.get("ha_moments") or {}).get("recent")


@pytest.mark.asyncio
async def test_admin_status_survives_moment_projection_failure():
    app = _make_test_app()
    store, _ = _store_with_aired_row()
    app.state.station_state.moment_store = store
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch.object(MomentStore, "to_admin_rows", side_effect=RuntimeError("boom")):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/status")

    assert resp.status_code == 200
    assert resp.json()["moments_admin"] == []


# --- stream hooks: mark_airing (send-start) --------------------------------------


def test_on_stream_segment_marks_moment_airing():
    state = StationState()
    store = MomentStore()
    moment_id = store.record(lane="directive", family="f", public_label="L", now=NOW)
    state.moment_store = store
    state.on_stream_segment(_banter_segment(ritual_moment_id=moment_id))
    assert store.rows[0].status == "airing"


def test_on_stream_segment_never_marks_rescue_or_fallback():
    state = StationState()
    store = MomentStore()
    rescue_id = store.record(lane="directive", family="f", public_label="L", now=NOW)
    fallback_id = store.record(lane="directive", family="f", public_label="L", now=NOW)
    state.moment_store = store
    state.on_stream_segment(_banter_segment(ritual_moment_id=rescue_id, rescue=True))
    state.on_stream_segment(_banter_segment(ritual_moment_id=fallback_id, audio_source="fallback_demo_asset"))
    assert {row.status for row in store.rows} == {"elected"}


def test_on_stream_segment_survives_store_failure():
    state = StationState()
    store = MomentStore()
    moment_id = store.record(lane="directive", family="f", public_label="L", now=NOW)
    state.moment_store = store
    with patch.object(MomentStore, "mark_airing", side_effect=RuntimeError("boom")):
        state.on_stream_segment(_banter_segment(ritual_moment_id=moment_id))
    # The stream bookkeeping still happened — a receipt bug never breaks audio.
    assert state.now_streaming["type"] == "banter"


# --- stream hooks: finalize (true outcome) ----------------------------------------


def _airing_store() -> tuple[MomentStore, str]:
    store = MomentStore()
    moment_id = store.record(lane="directive", family="f", public_label="L", now=NOW)
    store.mark_airing(moment_id, now=NOW + 1)
    return store, moment_id


def test_finalize_records_aired_on_clean_send():
    state = StationState()
    state.moment_store, moment_id = _airing_store()
    segment = _banter_segment(ritual_moment_id=moment_id)
    _finalize_moment_receipts(state, segment, bytes_sent=4096, was_skipped=False, listeners=2)
    assert state.moment_store.rows[0].status == "aired"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"bytes_sent": 4096, "was_skipped": True, "listeners": 2}, "skipped"),
        ({"bytes_sent": 0, "was_skipped": False, "listeners": 2}, "not_streamed"),
        ({"bytes_sent": 4096, "was_skipped": False, "listeners": 0}, "no_listeners"),
    ],
)
def test_finalize_uses_true_outcome_vocabulary(kwargs, expected):
    state = StationState()
    state.moment_store, moment_id = _airing_store()
    segment = _banter_segment(ritual_moment_id=moment_id)
    _finalize_moment_receipts(state, segment, **kwargs)
    assert state.moment_store.rows[0].status == expected


@pytest.mark.parametrize(
    "rescue_metadata",
    [
        {"rescue": True},
        {"queue_drain_recovery": True},
        {"audio_source": "fallback_demo_asset"},
    ],
)
def test_finalize_classifies_rescue_sends_as_fallback_never_aired(rescue_metadata):
    """S2: every documented rescue flag must route through is_fallback_active —
    a clean send of backup audio is fallback_rescue, never aired."""
    state = StationState()
    state.moment_store, moment_id = _airing_store()
    segment = _banter_segment(ritual_moment_id=moment_id, **rescue_metadata)
    _finalize_moment_receipts(state, segment, bytes_sent=4096, was_skipped=False, listeners=2)
    assert state.moment_store.rows[0].status == "fallback_rescue"


def test_lifecycle_guards_are_silent_noops():
    """Edge guards: empty status, unknown ids, and wrong-state transitions."""
    store = MomentStore()
    moment_id = store.record(lane="directive", family="f", public_label="L", now=NOW)
    store.finalize(moment_id, "", now=NOW + 1)  # empty status — ignored
    assert store.rows[0].status == "elected"
    store.mark_dropped("unknown-id", "whatever", now=NOW + 2)  # unknown — no raise
    store.mark_dropped(moment_id, "muted", now=NOW + 3)
    store.mark_airing(moment_id, now=NOW + 4)  # dropped rows can't air
    assert store.rows[0].status == "dropped"
    # A row finalized while still elected (mark_airing swallowed upstream)
    # falls back to final_ts/ts for its public age — and still shows.
    second = store.record(lane="directive", family="f", public_label="Second", now=NOW + 10)
    store.finalize(second, "aired", now=NOW + 70)
    (row,) = store.to_public_rows(now=NOW + 130)
    assert row == {"label": "Second", "ago_min": 1, "status": "aired"}


def test_finalize_runs_with_provenance_ledger_off():
    state = StationState()
    assert getattr(state, "ledger", None) is None  # Show Memory off (standalone default)
    state.moment_store, moment_id = _airing_store()
    segment = _banter_segment(ritual_moment_id=moment_id)
    _finalize_moment_receipts(state, segment, bytes_sent=4096, was_skipped=False, listeners=1)
    assert state.moment_store.rows[0].status == "aired"


def test_finalize_never_writes_to_disk(tmp_path):
    # The playback loop only mutates in memory; the producer's save site flushes.
    state = StationState()
    state.moment_store, moment_id = _airing_store()
    state.moment_store.save_if_dirty(tmp_path)
    mtime = (tmp_path / STORE_FILENAME).stat().st_mtime
    segment = _banter_segment(ritual_moment_id=moment_id)
    _finalize_moment_receipts(state, segment, bytes_sent=4096, was_skipped=False, listeners=1)
    assert (tmp_path / STORE_FILENAME).stat().st_mtime == mtime  # no write happened
    assert state.moment_store._dirty is True  # flushed later by the producer


def test_finalize_survives_store_failure():
    state = StationState()
    state.moment_store, moment_id = _airing_store()
    segment = _banter_segment(ritual_moment_id=moment_id)
    with patch.object(MomentStore, "finalize", side_effect=RuntimeError("boom")):
        _finalize_moment_receipts(state, segment, bytes_sent=4096, was_skipped=False, listeners=1)
    assert state.moment_store.rows[0].status == "airing"  # untouched, no exception


def test_finalize_ignores_segments_without_moment_ids():
    state = StationState()
    state.moment_store, _ = _airing_store()
    _finalize_moment_receipts(state, _banter_segment(), bytes_sent=4096, was_skipped=False, listeners=1)
    assert state.moment_store.rows[0].status == "airing"


# --- post-restart continuity (audio-delivery scenario 3) ---------------------------


@pytest.mark.asyncio
async def test_aired_moments_survive_restart(tmp_path):
    """The strip must not blank after an addon update: save → fresh load → still visible."""
    store, _ = _store_with_aired_row()
    store.save_if_dirty(tmp_path)

    app = _make_test_app()
    _enable_ha(app)
    with patch("mammamiradio.home.moment_receipts.time.time", return_value=NOW + 300):
        app.state.station_state.moment_store = MomentStore.load(tmp_path)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/public-status")).json()

    assert body["ha_moments"]["recent"][0]["label"] == "Morning launch"


def test_restart_demotes_live_rows_honestly(tmp_path):
    """Elected and airing rows can never reach air after a restart (the pending
    directive and offered gag were in-memory; the airing finalize died with the
    playback loop) — load demotes them so the admin trail never claims
    'waiting for its break' or 'on air right now' about a dead moment."""
    store = MomentStore()
    elected = store.record(lane="directive", family="f", public_label="Elected", now=NOW)
    airing = store.record(lane="interrupt", family="f", public_label="Airing", now=NOW)
    store.mark_airing(airing, now=NOW + 1)
    aired = store.record(lane="directive", family="f", public_label="Aired", now=NOW)
    store.mark_airing(aired, now=NOW + 2)
    store.finalize(aired, "aired", now=NOW + 3)
    store.save_if_dirty(tmp_path)

    with patch("mammamiradio.home.moment_receipts.time.time", return_value=NOW + 300):
        reloaded = MomentStore.load(tmp_path)

    by_id = {row.id: row for row in reloaded.rows}
    assert by_id[elected].status == "dropped" and by_id[elected].drop_reason == "restart"
    assert by_id[airing].status == "dropped" and by_id[airing].drop_reason == "restart"
    assert by_id[aired].status == "aired"  # finished rows are untouched
    # The demotion persists via the producer's next save (store left dirty).
    assert reloaded._dirty is True
    # And nothing demoted ever reaches the listener strip.
    assert reloaded.to_public_rows(now=NOW + 360) == [{"label": "Aired", "ago_min": 6, "status": "aired"}]


# --- live mute demotes receipts honestly -------------------------------------------


def test_mute_demotes_all_pending_receipts_and_clears_ids():
    """A live entity mute must kill every in-flight receipt — pending directive,
    offered gag, AND a directive already consumed into an in-flight generation
    (handoff slot) — or the muted moment still earns 'aired'."""
    from mammamiradio.web.streamer import _clear_home_context_usage

    app = _make_test_app()
    state = app.state.station_state
    config = app.state.config
    store = MomentStore()
    directive_id = store.record(lane="directive", family="f", public_label="Pending", now=NOW)
    gag_id = store.record(lane="running_gag", family="f", public_label="Gag", now=NOW)
    handoff_id = store.record(lane="directive", family="f", public_label="InFlight", now=NOW)
    state.moment_store = store
    state.ha_pending_directive = "some directive"
    state.ha_pending_directive_moment_id = directive_id
    state.ha_running_gag = "some gag"
    state.ha_running_gag_key = "bucket|off->on"
    state.ha_running_gag_moment_id = gag_id
    state.last_banter_ritual_moment_id = handoff_id

    _clear_home_context_usage(state, config)

    assert {row.drop_reason for row in store.rows} == {"muted"}
    assert {row.status for row in store.rows} == {"dropped"}
    assert state.ha_pending_directive_moment_id == ""
    assert state.ha_running_gag_moment_id == ""
    assert state.last_banter_ritual_moment_id == ""


# --- opaque-id leak guard ----------------------------------------------------------


@pytest.mark.asyncio
async def test_moment_metadata_adds_no_new_public_keys_beyond_opaque_ids():
    """now_streaming metadata reaches public payloads; receipt ids stay internal."""
    app = _make_test_app()
    state = app.state.station_state
    store, moment_id = _store_with_aired_row()
    state.moment_store = store
    state.on_stream_segment(_banter_segment(ritual_moment_id=moment_id))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/public-status")).json()

    ns_meta = (body.get("now_streaming") or {}).get("metadata") or {}
    moment_keys = {key for key in ns_meta if "moment" in key or "gag" in key or "ritual" in key}
    assert moment_keys == set()

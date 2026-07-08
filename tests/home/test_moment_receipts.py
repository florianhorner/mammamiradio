"""Moment Receipts store: lifecycle, bounds, persistence, and safety contract."""

from __future__ import annotations

import json

import pytest

from mammamiradio.home.moment_receipts import (
    MAX_ROWS,
    RETENTION_SECONDS,
    STORE_FILENAME,
    MomentStore,
)

NOW = 1_800_000_000.0


def _elected(store: MomentStore, *, lane: str = "directive", now: float = NOW) -> str:
    return store.record(
        lane=lane,
        family="morning_launch",
        public_label="Morning launch",
        entity_id="switch.coffee_machine",
        confidence=0.8,
        now=now,
    )


# --- record / lifecycle -------------------------------------------------------


def test_record_elected_and_dropped_rows():
    store = MomentStore()
    eid = _elected(store)
    did = store.record(
        lane="directive",
        family="cooking_kitchen",
        public_label="Kitchen ritual",
        status="dropped",
        drop_reason="directive_slot_busy",
        now=NOW,
    )
    rows = {r.id: r for r in store.rows}
    assert rows[eid].status == "elected"
    assert rows[did].status == "dropped"
    assert rows[did].drop_reason == "directive_slot_busy"
    assert eid != did and len(eid) == 12


def test_airing_then_finalize_aired():
    store = MomentStore()
    mid = _elected(store)
    store.mark_airing(mid, now=NOW + 60)
    assert store.rows[0].status == "airing"
    store.finalize(mid, "aired", now=NOW + 120)
    assert store.rows[0].status == "aired"
    assert store.rows[0].final_ts == NOW + 120


@pytest.mark.parametrize("outcome", ["skipped", "no_listeners", "not_streamed", "fallback_rescue"])
def test_finalize_uses_stream_outcome_vocabulary_verbatim(outcome):
    store = MomentStore()
    mid = _elected(store)
    store.mark_airing(mid, now=NOW + 1)
    store.finalize(mid, outcome, now=NOW + 2)
    assert store.rows[0].status == outcome


def test_finalize_unknown_id_and_double_finalize_are_noops():
    store = MomentStore()
    mid = _elected(store)
    store.finalize("nonexistent", "aired", now=NOW)  # must not raise
    store.mark_airing(mid, now=NOW + 1)
    store.finalize(mid, "aired", now=NOW + 2)
    store.finalize(mid, "skipped", now=NOW + 3)  # already final — ignored
    assert store.rows[0].status == "aired"
    store.mark_airing(mid, now=NOW + 4)  # final rows can't re-air
    assert store.rows[0].status == "aired"


def test_mark_dropped_demotes_only_elected_rows():
    store = MomentStore()
    mid = _elected(store)
    store.mark_dropped(mid, "generation_failed", now=NOW + 1)
    assert store.rows[0].status == "dropped"
    assert store.rows[0].drop_reason == "generation_failed"
    # An airing row is past the point of dropping.
    mid2 = _elected(store)
    store.mark_airing(mid2, now=NOW + 2)
    store.mark_dropped(mid2, "too_late", now=NOW + 3)
    assert store._find(mid2).status == "airing"


# --- bounds ---------------------------------------------------------------------


def test_cap_evicts_oldest_rows():
    store = MomentStore()
    ids = [_elected(store, now=NOW + i) for i in range(MAX_ROWS + 10)]
    assert len(store.rows) == MAX_ROWS
    kept_ids = {r.id for r in store.rows}
    assert ids[0] not in kept_ids and ids[-1] in kept_ids


def test_retention_prunes_stale_rows():
    store = MomentStore()
    old = _elected(store, now=NOW - RETENTION_SECONDS - 60)
    fresh = _elected(store, now=NOW - 60)
    _elected(store, now=NOW)  # a new record triggers the prune at `now`
    ids = {r.id for r in store.rows}
    assert old not in ids and fresh in ids


# --- public / admin projections --------------------------------------------------


def test_public_rows_show_aired_only_generic_fields():
    store = MomentStore()
    mid = _elected(store)
    store.mark_airing(mid, now=NOW + 30)
    store.finalize(mid, "aired", now=NOW + 60)
    _elected(store)  # still elected — never public
    rows = store.to_public_rows(now=NOW + 120)
    assert rows == [{"label": "Morning launch", "ago_min": 2, "status": "aired"}]
    # Privacy: no entity ids, confidence, families, or raw ids in public rows.
    assert set(rows[0]) == {"label", "ago_min", "status"}


def test_public_rows_include_airing_only_while_active():
    store = MomentStore()
    mid = _elected(store)
    store.mark_airing(mid, now=NOW + 5)
    assert store.to_public_rows(now=NOW + 10) == []
    active = store.to_public_rows(now=NOW + 10, active_ids={mid})
    assert active[0]["status"] == "airing"


def test_public_rows_exclude_non_aired_outcomes():
    store = MomentStore()
    for outcome in ("skipped", "no_listeners", "not_streamed", "fallback_rescue"):
        mid = _elected(store)
        store.mark_airing(mid, now=NOW)
        store.finalize(mid, outcome, now=NOW + 1)
    assert store.to_public_rows(now=NOW + 60) == []


def test_public_rows_capped_and_newest_first():
    store = MomentStore()
    for i in range(5):
        mid = _elected(store, now=NOW + i)
        store.mark_airing(mid, now=NOW + i)
        store.finalize(mid, "aired", now=NOW + i)
    rows = store.to_public_rows(now=NOW + 600)
    assert len(rows) == 3


def test_admin_rows_full_detail_capped():
    store = MomentStore()
    for i in range(30):
        _elected(store, now=NOW + i)
    rows = store.to_admin_rows(limit=25)
    assert len(rows) == 25
    assert rows[0]["ts"] > rows[-1]["ts"]  # newest first
    assert {"id", "lane", "family", "entity_id", "confidence", "status"} <= set(rows[0])


# --- persistence ------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path):
    store = MomentStore()
    mid = _elected(store)
    store.mark_airing(mid, now=NOW)
    store.finalize(mid, "aired", now=NOW + 1)
    store.save_if_dirty(tmp_path)
    loaded = MomentStore.load(tmp_path)
    assert [r.to_dict() for r in loaded.rows] == [r.to_dict() for r in store.rows]


def test_save_is_atomic_and_dirty_gated(tmp_path):
    store = MomentStore()
    store.save_if_dirty(tmp_path)  # clean — writes nothing
    assert not (tmp_path / STORE_FILENAME).exists()
    _elected(store)
    store.save_if_dirty(tmp_path)
    assert (tmp_path / STORE_FILENAME).exists()
    assert not list(tmp_path.glob("*.tmp"))
    mtime = (tmp_path / STORE_FILENAME).stat().st_mtime
    store.save_if_dirty(tmp_path)  # clean again — no rewrite
    assert (tmp_path / STORE_FILENAME).stat().st_mtime == mtime


@pytest.mark.parametrize("content", ["{not json", '"a string"', '{"rows": "nope"}'])
def test_load_corrupt_or_wrong_shape_starts_fresh(tmp_path, content):
    (tmp_path / STORE_FILENAME).write_text(content, encoding="utf-8")
    store = MomentStore.load(tmp_path)
    assert store.rows == []


def test_load_missing_file_starts_fresh(tmp_path):
    assert MomentStore.load(tmp_path).rows == []


def test_load_skips_malformed_rows_keeps_good_ones(tmp_path):
    good = {"id": "abc123def456", "ts": NOW, "lane": "directive", "family": "f", "public_label": "L"}
    payload = {"schema_version": 1, "rows": [good, "junk", {"id": ""}, {"ts": "not-a-number", "id": "x"}]}
    (tmp_path / STORE_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    # Malformed rows are skipped without raising; the parsable good row survives.
    loaded = MomentStore.load(tmp_path)
    assert [r.id for r in loaded.rows] == ["abc123def456"]


def test_save_failure_never_raises(tmp_path):
    store = MomentStore()
    _elected(store)
    target = tmp_path / "gone"
    store.save_if_dirty(target)  # directory doesn't exist — logged, not raised
    assert store._dirty is True  # stays dirty for a later retry


def test_store_is_cache_protected():
    from mammamiradio.playlist.downloader import _CACHE_PROTECTED

    assert STORE_FILENAME in _CACHE_PROTECTED


# --- evening ledger ritual_family contract (gag receipts depend on this) ---------


def test_ritual_family_threads_home_event_to_bucket_and_persists(tmp_path):
    from mammamiradio.home.evening_memory import EveningLedger
    from mammamiradio.home.ha_enrichment import HomeEvent

    ledger = EveningLedger()
    event = HomeEvent(
        entity_id="binary_sensor.fridge_door",
        label="Kitchen ritual",
        old_state="chiuso",
        new_state="aperto",
        timestamp=NOW,
        raw_old_state="off",
        raw_new_state="on",
        force_gag_candidate=True,
        gag_cooldown_seconds=3600.0,
        ritual_family="fridge_freezer_raid",
    )
    ledger.observe([event], now=NOW)
    (bucket,) = ledger.buckets.values()
    assert bucket.ritual_family == "fridge_freezer_raid"
    ledger.save_if_dirty(tmp_path)
    reloaded = EveningLedger.load(tmp_path)
    (rebucket,) = reloaded.buckets.values()
    assert rebucket.ritual_family == "fridge_freezer_raid"


def test_ritual_family_upgrades_plain_bucket_never_downgrades():
    from mammamiradio.home.evening_memory import EveningLedger
    from mammamiradio.home.ha_enrichment import HomeEvent

    ledger = EveningLedger()
    plain = HomeEvent(
        entity_id="switch.fan",
        label="Ventilatore",
        old_state="spento",
        new_state="acceso",
        timestamp=NOW,
        raw_old_state="off",
        raw_new_state="on",
    )
    ledger.observe([plain], now=NOW)
    (bucket,) = ledger.buckets.values()
    assert bucket.ritual_family == ""
    ritual = HomeEvent(
        entity_id="switch.fan",
        label="Bathroom ritual",
        old_state="spento",
        new_state="acceso",
        timestamp=NOW + 10,
        raw_old_state="off",
        raw_new_state="on",
        force_gag_candidate=True,
        ritual_family="shower_bathroom",
    )
    plain_later = HomeEvent(
        entity_id="switch.fan",
        label="Ventilatore",
        old_state="spento",
        new_state="acceso",
        timestamp=NOW + 20,
        raw_old_state="off",
        raw_new_state="on",
    )
    ledger.observe([ritual, plain_later], now=NOW + 30)
    (bucket,) = ledger.buckets.values()
    assert bucket.ritual_family == "shower_bathroom"

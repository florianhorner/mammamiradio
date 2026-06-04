"""Tests for the evening running-gag ledger (Impossible Moments v2, Approach A).

Covers the mandatory audio-delivery scenarios at the ledger layer (where the
logic lives):
  S1 Normal        — events accumulate, a gag renders.
  S2 Empty         — empty/no-event ledger renders nothing, never crashes.
  S3 Post-restart  — save → load resumes the same session and gags.
  S3b Corrupt      — a garbage ledger file starts fresh, never crashes boot.
Plus codex's high-risk cases: cache-hit double-counting, numeric bucket
explosion, restart-preserved cooldown.
"""

from __future__ import annotations

import datetime
import json
import random

from mammamiradio.home.evening_memory import (
    EVENING_GAP_SECONDS,
    GAG_COOLDOWN_SECONDS,
    LEDGER_FILENAME,
    MIN_COUNT_FOR_GAG,
    EveningLedger,
    GagBucket,
    _render_gag,
)
from mammamiradio.home.ha_enrichment import HomeEvent
from mammamiradio.playlist.downloader import _CACHE_PROTECTED

BASE = 1_780_000_000.0  # fixed epoch — deterministic ages

COFFEE = "switch.bar_kaffeemaschine_steckdose"  # GOLD, allowlisted
WASHER = "switch.bad_gross_waschmaschine_steckdose"  # SILVER, allowlisted
POWER = "sensor.haushalt_stromverbrauch_gesamt"  # SILVER, numeric — never a gag
PERSON = "person.florian_horner"  # GOLD but excluded
LIGHT = "light.magic_areas_light_groups_wohnzimmer_all_lights"  # not allowlisted


def ev(entity_id, raw_old, raw_new, ts, *, label="Etichetta", old="prima", new="dopo"):
    return HomeEvent(
        entity_id=entity_id,
        label=label,
        old_state=old,
        new_state=new,
        timestamp=ts,
        raw_old_state=raw_old,
        raw_new_state=raw_new,
    )


# --- observe / aggregation ---------------------------------------------------


def test_observe_new_event_creates_bucket():
    led = EveningLedger()
    changed = led.observe([ev(COFFEE, "off", "on", BASE + 1)], now=BASE + 1)
    assert changed is True
    assert led.session_id == 1
    assert len(led.buckets) == 1
    assert next(iter(led.buckets.values())).count == 1


def test_observe_watermark_dedupes_reserved_events():
    """fetch_home_context re-serves the same 30-min deque; we must count once."""
    led = EveningLedger()
    events = [ev(COFFEE, "off", "on", BASE + 1)]
    led.observe(events, now=BASE + 1)
    led.observe(events, now=BASE + 2)  # same deque, later poll
    led.observe(events, now=BASE + 3)
    assert sum(b.count for b in led.buckets.values()) == 1


def test_observe_increments_across_distinct_polls():
    led = EveningLedger()
    led.observe([ev(COFFEE, "off", "on", BASE + 1)], now=BASE + 1)
    led.observe([ev(COFFEE, "off", "on", BASE + 120)], now=BASE + 120)
    [bucket] = led.buckets.values()
    assert bucket.count == 2
    assert bucket.first_ts == BASE + 1
    assert bucket.last_ts == BASE + 120


def test_numeric_states_excluded_even_when_allowlisted():
    led = EveningLedger()
    led.observe([ev(COFFEE, "120.4", "131.9", BASE + 1)], now=BASE + 1)
    assert led.buckets == {}


def test_power_sensor_does_not_explode_buckets():
    led = EveningLedger()
    for i in range(50):
        led.observe([ev(POWER, str(200 + i), str(201 + i), BASE + i + 1)], now=BASE + i + 1)
    assert led.buckets == {}  # numeric drift never aggregates


def test_non_allowlisted_and_person_excluded():
    led = EveningLedger()
    led.observe([ev(LIGHT, "off", "on", BASE + 1)], now=BASE + 1)
    led.observe([ev(PERSON, "away", "home", BASE + 2)], now=BASE + 2)
    assert led.buckets == {}


# --- salience ----------------------------------------------------------------


def test_salience_gold_beats_silver_same_count():
    gold = GagBucket(COFFEE, "Caffè", "spento", "acceso", count=3, last_ts=BASE)
    silver = GagBucket(WASHER, "Lavatrice", "spento", "acceso", count=3, last_ts=BASE)
    assert gold.salience(now=BASE) > silver.salience(now=BASE)


def test_salience_rises_with_count_and_decays_with_age():
    low = GagBucket(COFFEE, "Caffè", "spento", "acceso", count=2, last_ts=BASE)
    high = GagBucket(COFFEE, "Caffè", "spento", "acceso", count=8, last_ts=BASE)
    assert high.salience(now=BASE) > low.salience(now=BASE)
    fresh = GagBucket(COFFEE, "Caffè", "spento", "acceso", count=3, last_ts=BASE)
    stale = GagBucket(COFFEE, "Caffè", "spento", "acceso", count=3, last_ts=BASE - 7200)
    assert fresh.salience(now=BASE) > stale.salience(now=BASE)


# --- session lifecycle -------------------------------------------------------


def test_session_continues_within_gap():
    led = EveningLedger()
    led.observe([ev(COFFEE, "off", "on", BASE + 1)], now=BASE + 1)
    led.observe([ev(COFFEE, "off", "on", BASE + 100)], now=BASE + 100)
    assert led.session_id == 1
    assert sum(b.count for b in led.buckets.values()) == 2


def test_session_rolls_after_inactivity_gap():
    led = EveningLedger()
    led.observe([ev(COFFEE, "off", "on", BASE + 1)], now=BASE + 1)
    later = BASE + 1 + EVENING_GAP_SECONDS + 60
    led.observe([ev(COFFEE, "off", "on", later)], now=later)
    assert led.session_id == 2
    # Old evening's tallies cleared; only the new event remains.
    assert sum(b.count for b in led.buckets.values()) == 1


def test_session_rolls_on_logical_day_boundary():
    # 3:00am and 4:30am same calendar day, gap < EVENING_GAP, but the 4am
    # rollover means they are different evenings.
    started = datetime.datetime(2026, 5, 30, 3, 0).timestamp()
    after = datetime.datetime(2026, 5, 30, 4, 30).timestamp()
    led = EveningLedger()
    led.observe([ev(COFFEE, "off", "on", started)], now=started)
    led.observe([ev(COFFEE, "off", "on", after)], now=after)
    assert led.session_id == 2


# --- selection / pacing ------------------------------------------------------


def _ledger_with_hot_gag(count=3):
    led = EveningLedger()
    led.buckets["k"] = GagBucket(COFFEE, "Caffè", "spento", "acceso", count=count, last_ts=BASE)
    led.watermark = BASE
    led.session_id = 1
    led.started_at = led.last_active = BASE
    return led


def test_select_fires_when_probability_one(monkeypatch):
    monkeypatch.setattr("mammamiradio.home.evening_memory.GAG_INJECT_PROBABILITY", 1.0)
    led = _ledger_with_hot_gag()
    out = led.select_and_render(now=BASE, rng=random.Random(0))
    assert out and "Caffè" in out
    assert led.buckets["k"].last_spoken_ts == BASE


def test_offer_gag_does_not_spend_cooldown_until_marked(monkeypatch):
    monkeypatch.setattr("mammamiradio.home.evening_memory.GAG_INJECT_PROBABILITY", 1.0)
    led = _ledger_with_hot_gag()
    offered = led.offer_gag(now=BASE, rng=random.Random(0))
    assert offered is not None
    key, rendered = offered
    assert key == "k"
    assert "Caffè" in rendered
    assert led.buckets["k"].last_spoken_ts == 0.0

    led.mark_spoken(key, now=BASE)

    assert led.buckets["k"].last_spoken_ts == BASE


def test_select_silent_when_probability_zero(monkeypatch):
    monkeypatch.setattr("mammamiradio.home.evening_memory.GAG_INJECT_PROBABILITY", 0.0)
    led = _ledger_with_hot_gag()
    assert led.select_and_render(now=BASE, rng=random.Random(0)) == ""
    assert led.buckets["k"].last_spoken_ts == 0.0  # no cooldown spent on silence


def test_cooldown_suppresses_refire_then_recovers(monkeypatch):
    monkeypatch.setattr("mammamiradio.home.evening_memory.GAG_INJECT_PROBABILITY", 1.0)
    led = _ledger_with_hot_gag()
    assert led.select_and_render(now=BASE, rng=random.Random(0))
    # Within cooldown → not eligible.
    assert led.select_and_render(now=BASE + GAG_COOLDOWN_SECONDS - 1, rng=random.Random(0)) == ""
    # After cooldown → eligible again.
    assert led.select_and_render(now=BASE + GAG_COOLDOWN_SECONDS + 1, rng=random.Random(0))


def test_below_min_count_never_fires(monkeypatch):
    monkeypatch.setattr("mammamiradio.home.evening_memory.GAG_INJECT_PROBABILITY", 1.0)
    led = _ledger_with_hot_gag(count=MIN_COUNT_FOR_GAG - 1)
    assert led.select_and_render(now=BASE, rng=random.Random(0)) == ""


def test_render_phrasing_by_count():
    light = GagBucket(COFFEE, "Caffè", "spento", "acceso", count=2)
    heavy = GagBucket(COFFEE, "Caffè", "spento", "acceso", count=4)
    assert "di nuovo stasera" in _render_gag(light)
    assert "non si ferma" in _render_gag(heavy)


# --- S2 empty fallback -------------------------------------------------------


def test_empty_ledger_renders_nothing():
    assert EveningLedger().select_and_render(now=BASE, rng=random.Random(0)) == ""


# --- persistence / S3 post-restart / S3b corrupt -----------------------------


def test_to_from_dict_roundtrip():
    led = _ledger_with_hot_gag()
    led.buckets["k"].last_spoken_ts = BASE
    restored = EveningLedger.from_dict(led.to_dict())
    assert restored.session_id == led.session_id
    assert restored.watermark == led.watermark
    assert restored.buckets["k"].count == led.buckets["k"].count
    assert restored.buckets["k"].last_spoken_ts == BASE


def test_save_and_load_resumes_session(tmp_path):
    led = EveningLedger()
    led.observe([ev(COFFEE, "off", "on", BASE + 1)], now=BASE + 1)
    led.observe([ev(COFFEE, "off", "on", BASE + 120)], now=BASE + 120)
    led.save_if_dirty(tmp_path)
    assert (tmp_path / LEDGER_FILENAME).exists()

    restored = EveningLedger.load(tmp_path)
    assert restored.session_id == 1
    assert sum(b.count for b in restored.buckets.values()) == 2
    # A new event shortly after restart resumes the SAME evening.
    restored.observe([ev(COFFEE, "off", "on", BASE + 200)], now=BASE + 200)
    assert restored.session_id == 1
    assert sum(b.count for b in restored.buckets.values()) == 3


def test_save_preserves_cooldown_across_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("mammamiradio.home.evening_memory.GAG_INJECT_PROBABILITY", 1.0)
    led = _ledger_with_hot_gag()
    led.select_and_render(now=BASE, rng=random.Random(0))
    led._dirty = True
    led.save_if_dirty(tmp_path)
    restored = EveningLedger.load(tmp_path)
    # Restart must NOT let the just-aired gag re-fire immediately.
    assert restored.select_and_render(now=BASE + 1, rng=random.Random(0)) == ""


def test_load_missing_starts_fresh(tmp_path):
    led = EveningLedger.load(tmp_path)
    assert led.session_id == 0
    assert led.buckets == {}


def test_load_corrupt_starts_fresh_without_crashing(tmp_path):
    (tmp_path / LEDGER_FILENAME).write_text("{ this is not valid json ")
    led = EveningLedger.load(tmp_path)
    assert led.session_id == 0
    assert led.buckets == {}


def test_load_wrong_shape_starts_fresh(tmp_path):
    (tmp_path / LEDGER_FILENAME).write_text(json.dumps([1, 2, 3]))
    led = EveningLedger.load(tmp_path)
    assert led.session_id == 0


def test_load_wrong_bucket_shape_starts_fresh(tmp_path):
    (tmp_path / LEDGER_FILENAME).write_text(json.dumps({"buckets": ["not", "an", "object"]}))
    led = EveningLedger.load(tmp_path)
    assert led.session_id == 0
    assert led.buckets == {}


def test_save_only_when_dirty(tmp_path):
    EveningLedger().save_if_dirty(tmp_path)  # fresh, not dirty
    assert not (tmp_path / LEDGER_FILENAME).exists()


def test_ledger_is_cache_protected():
    assert LEDGER_FILENAME in _CACHE_PROTECTED

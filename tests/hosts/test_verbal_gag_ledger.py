"""Tests for the verbal running-gag ledger (cross-domain banter callbacks)."""

import random

from mammamiradio.hosts.verbal_gag_ledger import (
    _DEFAULT_PUNCH,
    MAX_LIVE_GAGS,
    VerbalGag,
    VerbalGagLedger,
)


def _offer_until_hit(ledger, *, now, contrasting_to, tries=60):
    """Offer repeatedly across seeds (the 0.55 silence roll makes one call flaky)."""
    for seed in range(tries):
        res = ledger.offer(now=now, contrasting_to=contrasting_to, rng=random.Random(seed))
        if res is not None:
            return res
    return None


def test_add_then_offer_cross_domain():
    led = VerbalGagLedger()
    led.add_gag("bathroom fans", punch=5, now=1000.0)
    hit = _offer_until_hit(led, now=1000.0, contrasting_to="sports")
    assert hit is not None and hit[1].text == "bathroom fans"


def test_same_domain_never_offered():
    led = VerbalGagLedger()
    led.add_gag("fans", punch=5, now=0.0, source_domain="host")
    # contrasting_to == source → eligible set empty → None regardless of rng.
    for seed in range(20):
        assert led.offer(now=0.0, contrasting_to="host", rng=random.Random(seed)) is None


def test_retire_after_one_travel():
    led = VerbalGagLedger()
    led.add_gag("fans", punch=5, now=0.0)
    gid = next(iter(led.gags))
    led.mark_spoken(gid, now=1.0)
    assert led.gags == {}  # retired gags are pruned
    for seed in range(20):
        assert led.offer(now=2.0, contrasting_to="sports", rng=random.Random(seed)) is None


def test_default_punch_on_bare_seed():
    led = VerbalGagLedger()
    led.add_gag("x", now=0.0)
    assert next(iter(led.gags.values())).punch == _DEFAULT_PUNCH


def test_punch_clamped_to_1_5():
    led = VerbalGagLedger()
    led.add_gag("hot", punch=99, now=0.0)
    led.add_gag("cold", punch=-5, now=0.0)
    by_text = {g.text: g.punch for g in led.gags.values()}
    assert by_text["hot"] == 5.0
    assert by_text["cold"] == 1.0


def test_dedupe_by_text():
    led = VerbalGagLedger()
    led.add_gag("same", punch=5, now=0.0)
    led.add_gag("same", punch=2, now=10.0)
    assert len(led.gags) == 1


def test_empty_text_is_noop():
    led = VerbalGagLedger()
    led.add_gag("   ", now=0.0)
    led.add_gag("", now=0.0)
    assert led.gags == {}


def test_cap_evicts_oldest():
    led = VerbalGagLedger()
    for i in range(MAX_LIVE_GAGS + 5):
        led.add_gag(f"g{i}", punch=3, now=float(i))
    assert len(led.gags) <= MAX_LIVE_GAGS
    texts = {g.text for g in led.gags.values()}
    assert "g0" not in texts  # oldest evicted
    assert f"g{MAX_LIVE_GAGS + 4}" in texts  # newest kept


def test_salience_prefers_higher_punch():
    hi = VerbalGag(id="1", text="hi", source_domain="host", punch=5.0, created_ts=100.0)
    lo = VerbalGag(id="2", text="lo", source_domain="host", punch=1.0, created_ts=100.0)
    assert hi.salience(now=100.0) > lo.salience(now=100.0)


def test_salience_decays_with_age():
    g = VerbalGag(id="1", text="x", source_domain="host", punch=5.0, created_ts=0.0)
    assert g.salience(now=0.0) > g.salience(now=7200.0)  # ~half after one half-life


def test_mark_spoken_unknown_id_is_noop():
    led = VerbalGagLedger()
    led.add_gag("x", punch=5, now=0.0)
    led.mark_spoken("nope", now=1.0)
    assert len(led.gags) == 1  # untouched

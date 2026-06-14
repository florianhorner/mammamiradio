"""Tests for the shared weighted-pick-with-silence primitive (gag_select)."""

import random

from mammamiradio.home.gag_select import weighted_offer


def _w(obj, now):
    """Weight = the numeric object itself (tests use float payloads)."""
    return obj


def test_empty_items_returns_none():
    assert weighted_offer([], now=0.0, inject_probability=1.0, weight=_w) is None


def test_zero_total_weight_returns_none():
    items = [("a", 0.0), ("b", 0.0)]
    assert weighted_offer(items, now=0.0, inject_probability=1.0, weight=_w) is None


def test_picks_when_probability_one():
    # inject_probability=1.0 → silence roll random() >= 1.0 is never true → always picks.
    res = weighted_offer([("a", 5.0)], now=0.0, inject_probability=1.0, weight=_w, rng=random.Random(1))
    assert res == ("a", 5.0)


def test_silent_when_probability_zero():
    # inject_probability=0.0 → random() >= 0.0 always true → always silent.
    res = weighted_offer([("a", 5.0)], now=0.0, inject_probability=0.0, weight=_w, rng=random.Random(1))
    assert res is None


def test_weighted_pick_favors_heavier_weight():
    items = [("light", 1.0), ("heavy", 100.0)]
    picks = [
        weighted_offer(items, now=0.0, inject_probability=1.0, weight=_w, rng=random.Random(seed)) for seed in range(40)
    ]
    heavy = sum(1 for p in picks if p and p[0] == "heavy")
    assert heavy >= 36  # ~99% weight on "heavy"


def test_accepts_module_random_when_rng_none():
    # rng=None falls back to the random module; should still return a result.
    random.seed(0)
    res = weighted_offer([("a", 5.0)], now=0.0, inject_probability=1.0, weight=_w)
    assert res == ("a", 5.0)

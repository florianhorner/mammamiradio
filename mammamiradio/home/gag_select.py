"""Shared gag-selection primitive.

Extracted from ``EveningLedger.offer_gag`` so the verbal running-gag ledger
(``hosts/verbal_gag_ledger.py``) can reuse the EXACT same weighted-pick-then-
silence behavior instead of reinventing it.

Deliberately narrow (Codex review): only the weighted pick + silence roll is
shared. Eligibility filtering and salience scoring stay in each ledger because
they differ — home buckets filter on count+cooldown and score by
tier x log(count) x recency; verbal gags filter on cooldown+not-traveled+domain
and score by punch x recency.

Pipeline (the order is contractual — both ledgers depend on it for deterministic
RNG under an injected ``rng``):

    eligible items ─► weights ─► sum<=0 guard ─► roll.choices ─► silence roll
                                                  (weighted)     (AFTER select)
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def weighted_offer(
    items: list[tuple[str, T]],
    *,
    now: float,
    inject_probability: float,
    weight: Callable[[T, float], float],
    rng: random.Random | None = None,
) -> tuple[str, T] | None:
    """Pick one ``(key, obj)`` by salience weight, then apply a silence roll.

    ``items`` must already be filtered to the eligible set. ``weight(obj, now)``
    returns the salience. Returns the chosen ``(key, obj)`` or ``None`` — spends
    nothing (the caller marks cooldown only after the gag actually airs).

    A silence chance (``roll.random() >= inject_probability``) is applied AFTER
    the weighted selection so gags stay "discovered, not announced". Accepts a
    ``random.Random`` or the ``random`` module (``roll = rng or random``), which
    keeps monkeypatched/seeded tests deterministic.
    """
    roll = rng or random
    if not items:
        return None
    weights = [weight(obj, now) for _, obj in items]
    if sum(weights) <= 0:
        return None
    chosen: tuple[str, T] = roll.choices(items, weights=weights, k=1)[0]
    if roll.random() >= inject_probability:
        return None  # stayed silent; no cooldown spent
    return chosen

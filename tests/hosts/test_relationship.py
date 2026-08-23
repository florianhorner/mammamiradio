"""Exchange-shape selection: rotation, precedence, lore cap, and recency."""

from __future__ import annotations

import random
from collections import Counter, deque

import mammamiradio.hosts.prompt_world as prompt_world
import mammamiradio.hosts.relationship as relationship_module
from mammamiradio.core.models import ChaosSubtype, StationState

_CLASSIC_SHAPE = "marco_runaway_giulia_contains"
_RARE_SHAPES = frozenset({"marco_surprisingly_right", "giulia_small_confession_marco_misreads"})
_CANONICAL_HOSTS = ("Marco", "Giulia")


def _select(
    state: StationState,
    *,
    host_names: tuple[str, ...] = _CANONICAL_HOSTS,
    chaos_subtype: ChaosSubtype | None = None,
    festival: bool = False,
    guest_invited: bool = False,
    rng: random.Random | None = None,
):
    # /api/hot-reload recreates leaf-module classes in place. Resolve symbols
    # through the module object so randomized tests never retain stale identities.
    return relationship_module.select_exchange_shape(
        state,
        host_names=host_names,
        chaos_subtype=chaos_subtype,
        festival=festival,
        guest_invited=guest_invited,
        rng=rng,
    )


def _eligible(*, rng: random.Random | None = None):
    return _select(
        StationState(),
        chaos_subtype=None,
        festival=False,
        guest_invited=False,
        rng=rng,
    )


def test_relationship_state_is_established_pair_with_authored_lore():
    rel = relationship_module.relationship_state(_CANONICAL_HOSTS)
    assert rel.arc_phase == relationship_module.ARC_PHASE_ESTABLISHED_PAIR
    assert rel.seed_lore == prompt_world.HOST_SEED_LORE
    assert len(rel.seed_lore) == 6
    assert relationship_module.relationship_state((" giULIA ", "MARCO")).seed_lore == prompt_world.HOST_SEED_LORE


def test_shape_banks_and_weights_stay_in_sync():
    assert set(prompt_world.EXCHANGE_SHAPE_WEIGHTS) == set(prompt_world.EXCHANGE_SHAPES)
    assert set(prompt_world.GENERIC_EXCHANGE_SHAPE_WEIGHTS) == set(prompt_world.GENERIC_EXCHANGE_SHAPES)
    assert prompt_world.HOST_SEED_LORE_BY_PAIR[frozenset({"giulia", "marco"})] == prompt_world.HOST_SEED_LORE


def test_eligible_break_always_returns_a_shape():
    selection = _eligible(rng=random.Random(0))
    assert selection.skip_reason is None
    assert selection.shape_id in prompt_world.EXCHANGE_SHAPES
    assert selection.directive == prompt_world.EXCHANGE_SHAPES[selection.shape_id]
    assert selection.lore_id is None or selection.lore_id in dict(prompt_world.HOST_SEED_LORE)
    if selection.lore_id is None:
        assert selection.lore_text is None
    else:
        assert selection.lore_text == dict(prompt_world.HOST_SEED_LORE)[selection.lore_id]


def test_shape_rotation_avoids_the_last_five():
    state = StationState()
    blocked = list(prompt_world.EXCHANGE_SHAPES)[:5]
    state.recent_shapes.extend(blocked)
    selection = _select(
        state,
        chaos_subtype=None,
        festival=False,
        guest_invited=False,
        rng=random.Random(1),
    )
    assert selection.shape_id not in blocked
    assert selection.shape_id in prompt_world.EXCHANGE_SHAPES


def test_rotation_falls_back_when_every_shape_is_recent():
    state = StationState()
    state.recent_shapes = deque(
        prompt_world.EXCHANGE_SHAPES,
        maxlen=len(prompt_world.EXCHANGE_SHAPES),
    )
    selection = _select(
        state,
        chaos_subtype=None,
        festival=False,
        guest_invited=False,
        rng=random.Random(2),
    )
    assert selection.shape_id in prompt_world.EXCHANGE_SHAPES
    assert selection.skip_reason is None


def test_chaos_subtype_skips_before_festival_and_guest():
    selection = _select(
        StationState(),
        chaos_subtype=ChaosSubtype.FOURTH_WALL,
        festival=True,
        guest_invited=True,
        rng=random.Random(0),
    )
    assert selection.shape_id is None
    assert selection.skip_reason == "chaos"


def test_chaos_mode_flag_skips_without_a_subtype():
    state = StationState()
    state.chaos_mode_active = True
    selection = _select(
        state,
        chaos_subtype=None,
        festival=True,
        guest_invited=True,
    )
    assert selection.skip_reason == "chaos"
    assert selection.shape_id is None


def test_festival_skips_before_guest():
    selection = _select(
        StationState(),
        chaos_subtype=None,
        festival=True,
        guest_invited=True,
    )
    assert selection.skip_reason == "festival"
    assert selection.shape_id is None


def test_guest_skip_only_when_invited():
    invited = _select(
        StationState(),
        chaos_subtype=None,
        festival=False,
        guest_invited=True,
    )
    rostered_but_closed = _select(
        StationState(),
        chaos_subtype=None,
        festival=False,
        guest_invited=False,
        rng=random.Random(3),
    )
    assert invited.skip_reason == "guest"
    assert invited.shape_id is None
    assert rostered_but_closed.skip_reason is None
    assert rostered_but_closed.shape_id in prompt_world.EXCHANGE_SHAPES


def test_custom_pair_uses_only_generic_shapes_and_never_seed_lore():
    selection = _select(
        StationState(),
        host_names=("Alice", "Bob"),
        rng=random.Random(1),
    )
    assert selection.shape_id in prompt_world.GENERIC_EXCHANGE_SHAPES
    assert selection.directive == prompt_world.GENERIC_EXCHANGE_SHAPES[selection.shape_id]
    assert "marco" not in selection.directive.casefold()
    assert "giulia" not in selection.directive.casefold()
    assert selection.lore_id is None
    assert selection.lore_text is None
    assert relationship_module.relationship_state(("Alice", "Bob")).seed_lore == ()


def test_non_pair_rosters_are_ineligible_without_shape_or_lore():
    for host_names in (("Solo",), ("Same", " same "), ("A", "B", "C")):
        selection = _select(StationState(), host_names=host_names, rng=random.Random(2))
        assert selection.shape_id is None
        assert selection.lore_id is None
        assert selection.skip_reason == "ineligible_roster"


def test_lore_is_at_most_one_item_and_usually_absent():
    rng = random.Random(4)
    lore_hits = 0
    for _ in range(200):
        selection = _select(
            StationState(),
            chaos_subtype=None,
            festival=False,
            guest_invited=False,
            rng=rng,
        )
        lore_ids = [selection.lore_id] if selection.lore_id else []
        assert len(lore_ids) <= 1
        lore_hits += len(lore_ids)
    assert 0 < lore_hits < 200


def test_recent_lore_is_avoided_when_sampling():
    state = StationState()
    blocked = [item_id for item_id, _text in prompt_world.HOST_SEED_LORE[:5]]
    state.recent_lore.extend(blocked)
    remaining = prompt_world.HOST_SEED_LORE[5][0]

    class _ForceLore(random.Random):
        def random(self) -> float:
            return 0.0

        def choice(self, seq):
            assert list(seq) == [remaining]
            return remaining

        def choices(self, population, weights=None, *, k=1):
            del weights
            return [population[0] for _ in range(k)]

    selection = _select(
        state,
        chaos_subtype=None,
        festival=False,
        guest_invited=False,
        rng=_ForceLore(0),
    )
    assert selection.lore_id == remaining


def test_exhausted_lore_window_rotates_from_the_oldest_item():
    state = StationState()
    lore_ids = [item_id for item_id, _text in prompt_world.HOST_SEED_LORE]
    state.recent_lore.extend(lore_ids)

    class _ForceOldest(random.Random):
        def random(self) -> float:
            return 0.0

        def choice(self, seq):
            return seq[0]

        def choices(self, population, weights=None, *, k=1):
            del weights
            return [population[0] for _ in range(k)]

    rng = _ForceOldest(0)
    first = _select(state, rng=rng)
    assert first.lore_id == lore_ids[0]
    relationship_module.record_shape_recency(state, first)

    second = _select(state, rng=rng)
    assert second.lore_id == lore_ids[1]


def test_classic_shape_outweighs_rare_exceptions():
    rng = random.Random(5)
    state = StationState()
    counts: Counter[str] = Counter()
    for _ in range(4000):
        selection = _select(
            state,
            chaos_subtype=None,
            festival=False,
            guest_invited=False,
            rng=rng,
        )
        assert selection.shape_id is not None
        counts[selection.shape_id] += 1
        relationship_module.record_shape_recency(state, selection)
    common_shapes = set(prompt_world.EXCHANGE_SHAPES) - _RARE_SHAPES
    assert max(counts[shape_id] for shape_id in _RARE_SHAPES) < min(counts[shape_id] for shape_id in common_shapes)
    assert prompt_world.EXCHANGE_SHAPE_WEIGHTS[_CLASSIC_SHAPE] == 1.45
    for shape_id in _RARE_SHAPES:
        assert prompt_world.EXCHANGE_SHAPE_WEIGHTS[shape_id] == 0.28


def test_record_shape_recency_is_a_noop_on_skip():
    state = StationState()
    skipped = relationship_module.ExchangeShapeSelection(None, None, None, None, "festival")
    relationship_module.record_shape_recency(state, skipped)
    assert list(state.recent_shapes) == []
    assert list(state.recent_lore) == []


def test_record_shape_recency_appends_shape_and_optional_lore():
    state = StationState()
    relationship_module.record_shape_recency(
        state,
        relationship_module.ExchangeShapeSelection(
            shape_id=_CLASSIC_SHAPE,
            directive=prompt_world.EXCHANGE_SHAPES[_CLASSIC_SHAPE],
            lore_id="demo_tape",
            lore_text="lore",
            skip_reason=None,
        ),
    )
    relationship_module.record_shape_recency(
        state,
        relationship_module.ExchangeShapeSelection(
            shape_id="temporary_alliance",
            directive=prompt_world.EXCHANGE_SHAPES["temporary_alliance"],
            lore_id=None,
            lore_text=None,
            skip_reason=None,
        ),
    )
    assert list(state.recent_shapes) == [_CLASSIC_SHAPE, "temporary_alliance"]
    assert list(state.recent_lore) == ["demo_tape"]

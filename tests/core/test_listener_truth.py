"""Tests for the final listener-facing truth vocabulary guard."""

from __future__ import annotations

import pytest

from mammamiradio.core.listener_truth import (
    contains_unsafe_listener_claims,
    find_unsafe_listener_claim,
    home_return_authority_for_directive,
)


@pytest.mark.parametrize(
    "text",
    [
        "Someone just tuned in.",
        "A listener just joined.",
        "A new listener joined us right now.",
        "You have just joined us.",
        "Thanks for tuning in.",
        "Welcome back, amici.",
        "Glad to see you back.",
        "You've come back.",
        "You came back to us.",
        "Thanks for coming back.",
        "Thanks for returning.",
        "Great to have you back.",
        "Good to have you with us again.",
        "Thanks for rejoining us.",
        "Happy you're back.",
        "You have rejoined us.",
        "The listener returned.",
        "Florian is back.",
        "Great to have Florian back.",
        "Sabrina is here again.",
        "Qualcuno si è appena sintonizzato.",
        "Qualcuno si è unito a noi.",
        "Ci ha appena raggiunto qualcuno.",
        "Grazie per esserti sintonizzato.",
        "Ti sei appena sintonizzato.",
        "Voi vi siete appena collegati.",
        "Si è collegato qualcuno.",
        "Un nuovo arrivo in studio.",
        "Finalmente qualcuno ci ascolta.",
        "Bentornato, resta con noi.",
        "Bentornati, amici.",
        "Ben ritrovata.",
        "Eccoti qui di nuovo.",
        "Qualcuno è tornato.",
        "Sabrina è tornata.",
        "Grazie per essere tornati.",
        "Che bello riaverti qui.",
        "Felici di riavervi con noi.",
        "Siete qui di nuovo.",
        "Che bello rivedere Sabrina qui.",
        "Florian è di nuovo qui.",
    ],
)
def test_arrival_and_return_claims_are_rejected(text: str):
    assert find_unsafe_listener_claim(text) is not None
    assert contains_unsafe_listener_claims([text]) is True


@pytest.mark.parametrize(
    "text",
    [
        "We've had company for a while; the studio is still arguing.",
        "Abbiamo compagnia da un po', e Studio B continua a litigare.",
        "The music is back after the break.",
        "I'll call you back after the record.",
        "Benvenuti alla serata di Mamma Mi Radio.",
    ],
)
def test_aggregate_or_unrelated_copy_remains_allowed(text: str):
    assert find_unsafe_listener_claim(text) is None
    assert contains_unsafe_listener_claims(text) is False


def test_named_resident_return_requires_curated_fact_and_same_line_name():
    authority = home_return_authority_for_directive(
        "ha:person.florian_horner",
        "Florian è appena tornato a casa. Un caloroso bentornato.",
    )
    assert authority is not None
    assert contains_unsafe_listener_claims("Bentornato Florian.", return_authority=authority) is False
    assert contains_unsafe_listener_claims("Welcome back, Florian.", return_authority=authority) is False
    assert contains_unsafe_listener_claims("Great to have Florian back.", return_authority=authority) is False
    assert contains_unsafe_listener_claims("Florian is back.", return_authority=authority) is False
    assert contains_unsafe_listener_claims("Bentornato.", return_authority=authority) is True
    assert contains_unsafe_listener_claims("Welcome back, you two — Florian.", return_authority=authority) is True
    assert contains_unsafe_listener_claims("Welcome back, friends — Florian.", return_authority=authority) is True
    assert contains_unsafe_listener_claims("Bentornati, amici — Florian.", return_authority=authority) is True
    assert (
        contains_unsafe_listener_claims("Welcome back — Florian has the next record.", return_authority=authority)
        is True
    )
    assert (
        contains_unsafe_listener_claims("Welcome back, Florian. Sabrina is back.", return_authority=authority) is True
    )
    assert (
        contains_unsafe_listener_claims(
            "Welcome back, Florian. Somebody just returned.",
            return_authority=authority,
        )
        is True
    )


def test_named_resident_authority_can_be_spent_on_only_one_line():
    authority = home_return_authority_for_directive(
        "ha:person.sabrina",
        "Sabrina è appena tornata a casa. Un caloroso bentornata Sabrina.",
    )
    assert authority is not None
    assert (
        contains_unsafe_listener_claims(
            ["Bentornata Sabrina.", "Welcome back, Sabrina."],
            return_authority=authority,
        )
        is True
    )


def test_door_unlock_and_non_curated_sources_never_authorize_return_copy():
    assert (
        home_return_authority_for_directive(
            "ha:lock.lock_ultra_8d3c",
            "The front door unlocked.",
        )
        is None
    )
    assert contains_unsafe_listener_claims("Bentornato Florian.") is True


def test_all_stock_spoken_fallback_inventories_are_listener_truth_safe():
    from mammamiradio.core.config import load_config
    from mammamiradio.hosts import context_cues, fallbacks, scriptwriter, transitions

    def _texts(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from _texts(child)
        elif isinstance(value, list | tuple | set | frozenset):
            for child in value:
                yield from _texts(child)

    stock_texts = [text for name, value in vars(fallbacks).items() if name.isupper() for text in _texts(value)]
    stock_texts.extend(_texts(context_cues._BEHAVIORAL_CUES))
    stock_texts.extend(_texts(context_cues._IMPOSSIBLE_LINES))
    stock_texts.extend(_texts(context_cues._IMPOSSIBLE_DAY_LINES))
    stock_texts.extend(_texts(context_cues._SEASONAL_CUES))
    stock_texts.extend(_texts(transitions._TRANSITION_STOCK_COPY))
    for super_italian_mode in (False, True):
        config = load_config()
        config.super_italian_mode = super_italian_mode
        stock_texts.extend(text for pool in scriptwriter._banter_fallback_pools(config) for _host, text in pool)
    assert stock_texts
    assert contains_unsafe_listener_claims(stock_texts) is False

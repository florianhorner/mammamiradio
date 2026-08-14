from mammamiradio.home.context_value import (
    classify_home_context_entity_ids,
    has_useful_home_context_entity_ids,
)


def test_context_value_distinguishes_empty_from_daylight_only() -> None:
    assert classify_home_context_entity_ids([]) == "empty"
    assert classify_home_context_entity_ids(["sun.ambient"]) == "ambient_only"
    assert classify_home_context_entity_ids(["sun.sun", "sun.ambient", ""]) == "ambient_only"


def test_context_value_treats_weather_or_household_facts_as_useful() -> None:
    assert has_useful_home_context_entity_ids(["sun.ambient", "weather.ambient"]) is True
    assert has_useful_home_context_entity_ids(["switch.coffee_machine"]) is True
    assert has_useful_home_context_entity_ids(["sun.ambient"]) is False

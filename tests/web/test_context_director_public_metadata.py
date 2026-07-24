from mammamiradio.web.status_payload import _public_segment_metadata


def test_public_metadata_recursively_removes_home_context_director_keys():
    public = _public_segment_metadata(
        {
            "title": "Banter",
            "home_fact_id": "opaque",
            "nested": {
                "home_fact_fingerprint": "secret",
                "lines": [{"text": "ciao", "home_fact_id": "opaque"}],
            },
        }
    )

    assert public == {"title": "Banter", "nested": {"lines": [{"text": "ciao"}]}}


def test_public_metadata_removes_listener_session_and_home_return_fences():
    public = _public_segment_metadata(
        {
            "title": "Companionship",
            "listener_session_epoch": 7,
            "listener_session_cue": "companionship",
            "home_return_fact_id": "resident-return-opaque",
        }
    )

    assert public == {"title": "Companionship"}


def test_public_metadata_scrubs_internal_keys_inside_a_tuple_branch():
    # _without_internal has a dedicated tuple branch; the most sensitive key
    # (the raw HA entity id) must be stripped even when nested inside a tuple.
    public = _public_segment_metadata(
        {
            "brand": "Radio",
            "sources": ({"home_fact_entity_id": "binary_sensor.office", "keep": 1},),
        }
    )

    assert public == {"brand": "Radio", "sources": ({"keep": 1},)}


def test_public_metadata_returns_empty_dict_for_non_dict_input():
    assert _public_segment_metadata(None) == {}
    assert _public_segment_metadata(["home_fact_id"]) == {}
    assert _public_segment_metadata("home_fact_id") == {}

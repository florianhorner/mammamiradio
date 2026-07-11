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

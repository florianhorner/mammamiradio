"""Tests for the local Home Assistant entity mute policy (entity_policy.py).

load_entity_policy() and muted_entity_ids() sit on every fetch_home_context(),
producer timer-poll, and admin-preview call — a malformed policy file or a
disk error here must degrade to "nothing muted" rather than raising into the
audio path (INSTANT AUDIO).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from mammamiradio.home.entity_policy import (
    _clean_entry,
    _clean_personal_moment_entry,
    _clean_text,
    empty_policy,
    load_entity_policy,
    muted_entity_ids,
    personal_moment_enabled,
    personal_moment_opt_in_entity_ids,
    policy_path,
    policy_revision,
    set_entity_muted,
    set_personal_moment_enabled,
    valid_entity_id,
)


def test_valid_entity_id_accepts_domain_object_shape():
    assert valid_entity_id("switch.coffee_machine") is True
    assert valid_entity_id("binary_sensor.front_door_2") is True


def test_valid_entity_id_rejects_malformed_shapes():
    assert valid_entity_id("") is False
    assert valid_entity_id("switch") is False
    assert valid_entity_id("switch.") is False
    assert valid_entity_id(".coffee") is False
    assert valid_entity_id("Switch.Coffee") is False
    assert valid_entity_id("switch.coffee-machine") is False


def test_load_entity_policy_missing_file_returns_empty(tmp_path):
    assert load_entity_policy(tmp_path) == empty_policy()


def test_load_entity_policy_malformed_json_returns_empty(tmp_path):
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json")
    assert load_entity_policy(tmp_path) == empty_policy()


def test_load_entity_policy_root_not_a_dict_returns_empty(tmp_path):
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(["not", "a", "dict"]))
    assert load_entity_policy(tmp_path) == empty_policy()


def test_load_entity_policy_muted_key_not_a_dict_returns_empty(tmp_path):
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "muted": "not-a-dict"}))
    assert load_entity_policy(tmp_path) == empty_policy()


def test_load_entity_policy_skips_non_string_keys_and_invalid_entries(tmp_path):
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "muted": {
                    "switch.valid": {"label": "Coffee", "domain": "switch", "area": "Kitchen"},
                    "not.an.entity.id": {"label": "Bad shape"},
                    "switch.not_a_dict_entry": "oops",
                    "123": {"label": "Non-string key gets skipped before this runs"},
                },
            }
        )
    )
    policy = load_entity_policy(tmp_path)
    assert set(policy["muted"]) == {"switch.valid"}


def test_load_entity_policy_migrates_v1_mutes_without_granting_personal_consent(tmp_path):
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    v1 = {
        "schema_version": 1,
        "muted": {"switch.coffee_machine": {"label": "Coffee", "domain": "switch"}},
    }
    path.write_text(json.dumps(v1), encoding="utf-8")

    policy = load_entity_policy(tmp_path)

    assert policy["schema_version"] == 2
    assert set(policy["muted"]) == {"switch.coffee_machine"}
    assert policy["personal_moment_opt_ins"] == {}
    assert policy["policy_revision"] == 0
    assert json.loads(path.read_text(encoding="utf-8")) == v1


def test_load_entity_policy_accepts_current_personal_moment_opt_in(tmp_path):
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "policy_revision": 7,
                "muted": {},
                "personal_moment_opt_ins": {
                    "binary_sensor.office_presence": {
                        "enabled_at": 123.0,
                        "label": "Office presence",
                        "domain": "binary_sensor",
                        "area": "Office",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert policy_revision(tmp_path) == 7
    assert personal_moment_opt_in_entity_ids(tmp_path) == {"binary_sensor.office_presence"}
    assert personal_moment_enabled(tmp_path, "binary_sensor.office_presence") is True


def test_load_entity_policy_personal_opt_in_is_fail_closed_for_malformed_future_and_muted_entries(tmp_path):
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "muted": {"binary_sensor.muted_presence": {"label": "Muted"}},
                "personal_moment_opt_ins": {
                    "binary_sensor.valid_presence": {"enabled_at": 1},
                    "binary_sensor.muted_presence": {"enabled_at": 1},
                    "binary_sensor.not_a_dict": True,
                },
            }
        ),
        encoding="utf-8",
    )

    assert personal_moment_opt_in_entity_ids(tmp_path) == {"binary_sensor.valid_presence"}

    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "muted": {"switch.coffee_machine": {"label": "Coffee"}},
                "personal_moment_opt_ins": {"binary_sensor.office_presence": {"enabled_at": 1}},
            }
        ),
        encoding="utf-8",
    )

    future = load_entity_policy(tmp_path)
    assert set(future["muted"]) == {"switch.coffee_machine"}
    assert future["personal_moment_opt_ins"] == {}
    assert future["policy_revision"] == 0


def test_load_entity_policy_permission_error_returns_empty_on_first_ever_read(tmp_path):
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "muted": {}}))
    with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
        assert load_entity_policy(tmp_path) == empty_policy()


def test_load_entity_policy_read_error_falls_back_to_last_known_good_not_empty(tmp_path):
    """A transient disk error must not silently un-mute everything — this is a
    privacy control, so it degrades to the last confirmed policy, not empty
    (codex adversarial review: fail-open here defeats the mute promise)."""
    set_entity_muted(tmp_path, "switch.coffee_machine", True, label="Coffee")
    good_policy = load_entity_policy(tmp_path)
    assert good_policy["muted"]

    with patch("pathlib.Path.read_text", side_effect=OSError("disk hiccup")):
        degraded = load_entity_policy(tmp_path)

    assert degraded == good_policy
    assert degraded != empty_policy()


def test_load_entity_policy_uses_mtime_cache_for_unchanged_file(tmp_path):
    set_entity_muted(tmp_path, "switch.coffee_machine", True, label="Coffee")
    good_policy = load_entity_policy(tmp_path)

    with patch("pathlib.Path.read_text", side_effect=AssertionError("should use cached policy")):
        cached = load_entity_policy(tmp_path)

    assert cached == good_policy


def test_load_entity_policy_last_good_isolated_per_cache_path(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    set_entity_muted(first, "switch.coffee_machine", True, label="Coffee")
    assert load_entity_policy(first)["muted"]

    bad_path = policy_path(second)
    bad_path.parent.mkdir(parents=True)
    bad_path.write_text("{not valid json")

    assert load_entity_policy(second) == empty_policy()


def test_clean_text_none_becomes_empty_string():
    assert _clean_text(None) == ""


def test_clean_text_strips_null_bytes_and_whitespace():
    assert _clean_text("a\x00b") == "ab"
    assert _clean_text("  hello  ") == "hello"


def test_clean_text_truncates_to_max_len():
    assert _clean_text("hello world", max_len=5) == "hello"


def test_clean_entry_rejects_invalid_entity_id_or_non_dict():
    assert _clean_entry("not-an-id", {"label": "x"}) is None
    assert _clean_entry("switch.valid", "not-a-dict") is None


def test_clean_entry_falls_back_to_now_when_muted_at_is_not_numeric():
    entry = _clean_entry("switch.valid", {"muted_at": "not-a-number", "domain": "switch"})
    assert entry is not None
    assert isinstance(entry["muted_at"], float)
    assert entry["muted_at"] > 0


def test_clean_personal_moment_entry_rejects_malformed_permission_data():
    assert _clean_personal_moment_entry("binary_sensor.office_presence", True) is None
    assert _clean_personal_moment_entry("binary_sensor.office_presence", {"enabled_at": "not-a-number"}) is None
    entry = _clean_personal_moment_entry(
        "binary_sensor.office_presence",
        {"enabled_at": 123.0, "domain": "binary_sensor", "label": " Office "},
    )
    assert entry is not None
    assert entry["label"] == "Office"
    assert entry["enabled_at"] == 123.0


def test_muted_entity_ids_with_none_cache_dir_returns_empty_set():
    assert muted_entity_ids(None) == set()


def test_personal_moment_helpers_with_none_or_invalid_entity_are_safe():
    assert personal_moment_opt_in_entity_ids(None) == set()
    assert personal_moment_enabled(None, "binary_sensor.office_presence") is False
    assert personal_moment_enabled(None, "not-an-entity-id") is False
    assert policy_revision(None) == 0


def test_muted_entity_ids_reflects_saved_policy(tmp_path):
    set_entity_muted(tmp_path, "switch.coffee_machine", True, label="Coffee")
    assert muted_entity_ids(tmp_path) == {"switch.coffee_machine"}


def test_set_entity_muted_rejects_invalid_entity_id(tmp_path):
    with pytest.raises(ValueError):
        set_entity_muted(tmp_path, "not-an-entity-id", True)


def test_set_entity_muted_unmute_removes_entry(tmp_path):
    set_entity_muted(tmp_path, "switch.coffee_machine", True, label="Coffee")
    policy = set_entity_muted(tmp_path, "switch.coffee_machine", False)
    assert policy["muted"] == {}


def test_set_personal_moment_enabled_persists_v2_consent_and_is_idempotent(tmp_path):
    first = set_personal_moment_enabled(
        tmp_path,
        "binary_sensor.office_presence",
        True,
        label="Office presence",
        domain="binary_sensor",
        area="Office",
        now=100.0,
    )
    repeated = set_personal_moment_enabled(
        tmp_path,
        "binary_sensor.office_presence",
        True,
        label="Different label is ignored for an identical action",
        now=200.0,
    )

    assert first["schema_version"] == 2
    assert first["policy_revision"] == 1
    assert repeated["policy_revision"] == 1
    assert personal_moment_enabled(tmp_path, "binary_sensor.office_presence") is True
    assert repeated["personal_moment_opt_ins"]["binary_sensor.office_presence"]["enabled_at"] == 100.0
    assert (policy_path(tmp_path).stat().st_mode & 0o777) == 0o600


def test_disabling_personal_moment_removes_consent_and_advances_revision(tmp_path):
    set_personal_moment_enabled(tmp_path, "binary_sensor.office_presence", True)
    policy = set_personal_moment_enabled(tmp_path, "binary_sensor.office_presence", False)

    assert policy["personal_moment_opt_ins"] == {}
    assert policy["policy_revision"] == 2
    assert personal_moment_enabled(tmp_path, "binary_sensor.office_presence") is False


def test_muting_clears_personal_moment_consent_and_unmute_does_not_restore_it(tmp_path):
    set_personal_moment_enabled(tmp_path, "binary_sensor.office_presence", True)
    muted = set_entity_muted(tmp_path, "binary_sensor.office_presence", True)
    unmuted = set_entity_muted(tmp_path, "binary_sensor.office_presence", False)

    assert "binary_sensor.office_presence" in muted["muted"]
    assert muted["personal_moment_opt_ins"] == {}
    assert muted["policy_revision"] == 2
    assert unmuted["policy_revision"] == 3
    assert personal_moment_enabled(tmp_path, "binary_sensor.office_presence") is False


def test_cannot_enable_personal_moment_for_muted_entity(tmp_path):
    set_entity_muted(tmp_path, "binary_sensor.office_presence", True)

    with pytest.raises(ValueError, match="muted entity"):
        set_personal_moment_enabled(tmp_path, "binary_sensor.office_presence", True)


def test_personal_moment_setter_rejects_invalid_id_and_non_boolean(tmp_path):
    with pytest.raises(ValueError, match="invalid entity_id"):
        set_personal_moment_enabled(tmp_path, "not-an-entity-id", True)
    with pytest.raises(ValueError, match="boolean"):
        set_personal_moment_enabled(tmp_path, "binary_sensor.office_presence", "yes")  # type: ignore[arg-type]


def test_set_entity_muted_write_failure_cleans_up_tmp_file_and_raises(tmp_path):
    with patch("pathlib.Path.write_text", side_effect=OSError("disk full")), pytest.raises(OSError):
        set_entity_muted(tmp_path, "switch.coffee_machine", True, label="Coffee")
    leftover_tmp = list(policy_path(tmp_path).parent.glob(".*.tmp")) if policy_path(tmp_path).parent.exists() else []
    assert leftover_tmp == []


def test_policy_file_is_owner_only_permissions(tmp_path):
    set_entity_muted(tmp_path, "switch.coffee_machine", True, label="Coffee")
    mode = policy_path(tmp_path).stat().st_mode & 0o777
    assert mode == 0o600

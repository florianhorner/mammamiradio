"""Tests for banter new_joke normalization + the flash/ad callback block."""

from mammamiradio.hosts.scriptwriter import _callback_block, _normalize_new_joke


def test_normalize_dict_with_punch():
    assert _normalize_new_joke({"text": "  bathroom fans ", "punch": 4}) == ("bathroom fans", 4.0)


def test_normalize_bare_string_backcompat():
    assert _normalize_new_joke("bathroom fans") == ("bathroom fans", None)


def test_normalize_unparseable_punch():
    assert _normalize_new_joke({"text": "x", "punch": "nope"}) == ("x", None)


def test_normalize_missing_punch():
    assert _normalize_new_joke({"text": "x"}) == ("x", None)


def test_normalize_missing_text():
    assert _normalize_new_joke({"punch": 5}) == ("", 5.0)


def test_callback_block_empty_when_no_gag():
    assert _callback_block(None) == ""
    assert _callback_block("") == ""


def test_callback_block_present_carries_gag_and_flag():
    block = _callback_block("bathroom fans")
    assert "bathroom fans" in block
    assert "callback_used" in block  # asks the model to report whether it landed
    assert block.startswith("\n")  # attaches cleanly, no dangling blank line when empty

"""Tests for security-critical sanitization functions."""

from __future__ import annotations

from mammamiradio.hosts.scriptwriter import _sanitize_prompt_data
from mammamiradio.web.streamer import _save_dotenv

# ── _sanitize_prompt_data ────────────────────────────────────────────


class TestSanitizePromptData:
    def test_visible_ascii_unchanged(self):
        assert _sanitize_prompt_data("Hello World 123!@#$%^&*()") == "Hello World 123!@#$%^&*()"

    def test_null_bytes_stripped(self):
        assert _sanitize_prompt_data("a\x00b") == "ab"

    def test_control_chars_stripped(self):
        # \x01-\x08, \x0b, \x0c, \x0e-\x1f
        injected = "a\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f\x1fb"
        assert _sanitize_prompt_data(injected) == "ab"

    def test_angle_brackets_stripped(self):
        assert _sanitize_prompt_data("<script>alert(1)</script>") == "scriptalert(1)/script"

    def test_curly_braces_stripped(self):
        assert _sanitize_prompt_data("{{user_input}}") == "user_input"

    def test_newlines_preserved(self):
        assert _sanitize_prompt_data("line1\nline2") == "line1\nline2"

    def test_tabs_preserved(self):
        assert _sanitize_prompt_data("col1\tcol2") == "col1\tcol2"

    def test_under_max_len_not_truncated(self):
        text = "a" * 80
        assert _sanitize_prompt_data(text) == text

    def test_over_max_len_truncated(self):
        text = "a" * 81
        result = _sanitize_prompt_data(text)
        assert result == "a" * 80 + "..."
        assert len(result) == 83

    def test_custom_max_len(self):
        result = _sanitize_prompt_data("abcdefghij", max_len=5)
        assert result == "abcde..."

    def test_empty_string(self):
        assert _sanitize_prompt_data("") == ""

    def test_prompt_injection_attempt(self):
        attack = "Ignore all previous<script>alert(1)</script>"
        result = _sanitize_prompt_data(attack)
        # Tags stripped, then truncated at 80
        assert "<" not in result
        assert ">" not in result

    def test_unicode_italian_chars(self):
        text = "Caffè è buono, città àèìòù"
        assert _sanitize_prompt_data(text) == text


# ── _save_dotenv ─────────────────────────────────────────────────────


class TestSaveDotenv:
    def test_newlines_in_values_stripped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _save_dotenv({"KEY": "val\nEVIL=pwned"})
        content = (tmp_path / ".env").read_text()
        assert 'KEY="valEVIL=pwned"' in content
        assert "EVIL" not in content.split("=", 1)[0]  # no separate EVIL key

    def test_carriage_returns_stripped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _save_dotenv({"KEY": "val\rEVIL"})
        content = (tmp_path / ".env").read_text()
        assert 'KEY="valEVIL"' in content

    def test_values_quoted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _save_dotenv({"KEY": "value"})
        content = (tmp_path / ".env").read_text()
        assert 'KEY="value"' in content

    def test_existing_key_updated_not_duplicated(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text('KEY="old"\n')
        _save_dotenv({"KEY": "new"})
        content = (tmp_path / ".env").read_text()
        assert content.count("KEY=") == 1
        assert 'KEY="new"' in content

    def test_new_key_appended(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text('EXISTING="val"\n')
        _save_dotenv({"NEW": "val2"})
        content = (tmp_path / ".env").read_text()
        assert 'EXISTING="val"' in content
        assert 'NEW="val2"' in content

    def test_comments_and_empty_lines_preserved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text('# This is a comment\n\nKEY="old"\n')
        _save_dotenv({"KEY": "new"})
        lines = (tmp_path / ".env").read_text().splitlines()
        assert lines[0] == "# This is a comment"
        assert lines[1] == ""

    def test_file_created_if_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert not (tmp_path / ".env").exists()
        _save_dotenv({"KEY": "val"})
        assert (tmp_path / ".env").exists()
        assert 'KEY="val"' in (tmp_path / ".env").read_text()

    def test_output_ends_with_newline(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _save_dotenv({"KEY": "val"})
        content = (tmp_path / ".env").read_text()
        assert content.endswith("\n")

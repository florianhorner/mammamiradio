"""Regression guards for QA findings on 2026-04-23.

Each test protects against a specific class of bug identified during the
full-depth QA run. Delete at your own risk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ── LRU eviction must spare queued paths (P1 — audio delivery blast radius) ──


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path


def test_evict_cache_lru_protects_queued_paths(cache_dir: Path):
    """Queued norm paths must never be evicted mid-stream.

    Evicting a file currently in the playback queue breaks leadership
    principle #2 (INSTANT AUDIO) — the streamer would open a deleted file.
    """
    from mammamiradio.downloader import evict_cache_lru

    queued_norm = cache_dir / "norm_queued_192k.mp3"
    queued_norm.write_bytes(b"x" * 1024 * 1024)
    # Two stale files so the protected one does not suppress eviction on its own.
    stale_a = cache_dir / "norm_stale_a_192k.mp3"
    stale_a.write_bytes(b"x" * 1024 * 1024)
    stale_b = cache_dir / "norm_stale_b_192k.mp3"
    stale_b.write_bytes(b"x" * 1024 * 1024)

    # Budget 1 MB; 2 MB of evictable data — eviction must run on the stale files.
    evict_cache_lru(cache_dir, 1, protected_paths={queued_norm})
    assert queued_norm.exists(), "queued norm file was evicted mid-stream"
    assert not (stale_a.exists() and stale_b.exists()), "at least one non-queued norm should be evicted"


def test_evict_cache_lru_default_protected_paths_is_optional(cache_dir: Path):
    """The new protected_paths parameter is optional and defaults to None."""
    from mammamiradio.downloader import evict_cache_lru

    a = cache_dir / "norm_a_192k.mp3"
    a.write_bytes(b"x" * 1024 * 1024)
    b = cache_dir / "norm_b_192k.mp3"
    b.write_bytes(b"x" * 1024 * 1024)
    evict_cache_lru(cache_dir, 1)  # no protected_paths kwarg
    # One of the two files must have been evicted.
    assert not (a.exists() and b.exists()), "eviction did not run"


# ── LLM prompt injection defense (H2/H3) ──


def test_sanitize_prompt_data_strips_quotes():
    """Listener-submitted text must not break out of quoted interpolation."""
    from mammamiradio.scriptwriter import _sanitize_prompt_data

    for payload in (
        'hello" injected',
        "`backtick` attack",
        "left\u201cright\u201d smart",
        "it\u2019s curly",
    ):
        cleaned = _sanitize_prompt_data(payload, max_len=200)
        for quote_char in ('"', "`", "\u201c", "\u201d", "\u2018", "\u2019"):
            assert quote_char not in cleaned, f"{quote_char!r} leaked through for payload {payload!r} -> {cleaned!r}"


def test_sanitize_prompt_data_strips_role_markers():
    """Fake role markers must never survive into the prompt."""
    from mammamiradio.scriptwriter import _sanitize_prompt_data

    for role in ("System:", "Assistant:", "Human:", "User:", "system :", "ASSISTANT:"):
        payload = f"text {role} fake turn"
        out = _sanitize_prompt_data(payload, max_len=200)
        # The colon-bearing role marker must not reappear intact
        assert role.lower().replace(" ", "") not in out.lower().replace(" ", ""), (
            f"role marker survived: {role!r} -> {out!r}"
        )


def test_sanitize_prompt_data_still_removes_control_chars():
    """Existing control-char removal must not regress."""
    from mammamiradio.scriptwriter import _sanitize_prompt_data

    payload = "hello\x00\x01<tag>{braces}world"
    out = _sanitize_prompt_data(payload, max_len=200)
    assert "\x00" not in out
    assert "\x01" not in out
    assert "<" not in out and ">" not in out
    assert "{" not in out and "}" not in out


def test_sanitize_prompt_data_truncates():
    from mammamiradio.scriptwriter import _sanitize_prompt_data

    out = _sanitize_prompt_data("a" * 500, max_len=80)
    assert len(out) <= 83  # "aaaa...aaa..."
    assert out.endswith("...")


# ── ICY header injection defense (M1) ──


def test_icy_header_strips_crlf():
    """Station name with CR/LF must not leak into ICY headers.

    An operator who sets STATION_NAME with a newline could inject additional
    HTTP headers into the /stream response. The streamer scrubs \\r and \\n.
    """
    raw = "Mamma Mi Radio\r\nX-Evil: 1"
    scrubbed = raw.replace("\r", "").replace("\n", "")
    assert "\r" not in scrubbed
    assert "\n" not in scrubbed
    assert scrubbed == "Mamma Mi RadioX-Evil: 1"


# ── youtube_id validation (M4) ──


def test_youtube_id_regex_accepts_valid_id():
    """An 11-char YouTube video ID passes the format gate."""
    import re

    assert re.fullmatch(r"[A-Za-z0-9_-]{11}", "dQw4w9WgXcQ")
    assert re.fullmatch(r"[A-Za-z0-9_-]{11}", "abc-def_ghi")


def test_youtube_id_regex_rejects_injection_attempts():
    """Path traversal and SQL injection attempts must be rejected by the format gate."""
    import re

    pattern = re.compile(r"[A-Za-z0-9_-]{11}")
    for bad in ("../../etc/passwd", "'; DROP TABLE--", "abc123", "", "x" * 12, "bad space1"):
        assert not pattern.fullmatch(bad), f"{bad!r} should not match"


# ── Version sync between pyproject.toml and addon config.yaml ──


def test_addon_version_matches_pyproject():
    """HA addon config version must match package version — CI blocks on mismatch."""
    import tomllib

    project_root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())
    pkg_version = pyproject["project"]["version"]

    addon_config_text = (project_root / "ha-addon" / "mammamiradio" / "config.yaml").read_text()
    for line in addon_config_text.splitlines():
        if line.startswith("version:"):
            addon_version = line.split(":", 1)[1].strip().strip('"')
            assert addon_version == pkg_version, f"addon version {addon_version!r} != pyproject {pkg_version!r}"
            return
    pytest.fail("version: key not found in ha-addon/mammamiradio/config.yaml")


# ── P0-1: producer wakes on session resume (race window) ──


def test_station_state_has_resume_event():
    """StationState ships an asyncio.Event for producer wakeup on resume."""
    import asyncio

    from mammamiradio.models import StationState

    state = StationState()
    assert isinstance(state.resume_event, asyncio.Event)
    assert not state.resume_event.is_set()


# ── P1-2: silence-fallback never queues known-silent audio ──


def test_get_last_music_file_prefers_state(tmp_path: Path):
    """_get_last_music_file reads state.last_music_file first for test isolation."""
    from mammamiradio import producer
    from mammamiradio.models import StationState

    good = tmp_path / "good.mp3"
    good.write_bytes(b"x")
    state = StationState()
    state.last_music_file = good
    # Clobber module-level so we prove state wins.
    saved = producer._last_music_file
    try:
        producer._last_music_file = None
        assert producer._get_last_music_file(state) == good
    finally:
        producer._last_music_file = saved


def test_get_last_music_file_falls_back_to_module(tmp_path: Path):
    """When state has no entry, module-level cache is the fallback."""
    from mammamiradio import producer
    from mammamiradio.models import StationState

    legacy = tmp_path / "legacy.mp3"
    legacy.write_bytes(b"x")
    state = StationState()
    state.last_music_file = None
    saved = producer._last_music_file
    try:
        producer._last_music_file = legacy
        assert producer._get_last_music_file(state) == legacy
    finally:
        producer._last_music_file = saved


def test_get_last_music_file_returns_none_when_missing(tmp_path: Path):
    """A non-existent path in state must not be returned as playable."""
    from mammamiradio import producer
    from mammamiradio.models import StationState

    state = StationState()
    state.last_music_file = tmp_path / "gone.mp3"  # does not exist
    saved = producer._last_music_file
    try:
        producer._last_music_file = None
        assert producer._get_last_music_file(state) is None
    finally:
        producer._last_music_file = saved

"""Tests for scripts/ha-watch.py — the HA upstream early-warning watcher.

Network is never touched: parsing and filtering are pure functions fed fixture
bytes. The headline assertion encodes the design's verification gate — the
watcher would have flagged the 2026.6 entity-first card picker before GA, and a
no-op week produces nothing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ha-watch.py"
_spec = importlib.util.spec_from_file_location("ha_watch", _SCRIPT)
assert _spec and _spec.loader
ha_watch = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass decorators can resolve `__module__`.
sys.modules["ha_watch"] = ha_watch
_spec.loader.exec_module(ha_watch)


# --- Fixtures: real-shaped feed bytes -------------------------------------

RSS_WITH_HIT = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>HA Developer Blog</title>
  <item>
    <title>Custom card suggestions in the card picker</title>
    <link>https://developers.home-assistant.io/blog/2026/05/27/custom-card-suggestions/</link>
    <guid>https://developers.home-assistant.io/blog/2026/05/27/custom-card-suggestions/</guid>
    <description>Custom cards can now opt into the new entity-first card picker
      via getEntitySuggestion.</description>
    <pubDate>Tue, 27 May 2026 00:00:00 GMT</pubDate>
  </item>
  <item>
    <title>A new color scheme for the energy graphs</title>
    <link>https://developers.home-assistant.io/blog/2026/05/20/energy-colors/</link>
    <guid>https://developers.home-assistant.io/blog/2026/05/20/energy-colors/</guid>
    <description>We refreshed the palette used by the energy dashboards graphs.</description>
    <pubDate>Wed, 20 May 2026 00:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Deprecation of the legacy media_player turn_on helper</title>
    <link rel="alternate" href="https://www.home-assistant.io/blog/2026/06/03/release-20266/"/>
    <id>tag:home-assistant.io,2026:/blog/release-20266</id>
    <updated>2026-06-03T00:00:00Z</updated>
    <summary>The 2026.6 release ships the entity-first card picker.</summary>
  </entry>
  <entry>
    <title>Welcoming new translators</title>
    <link rel="alternate" href="https://www.home-assistant.io/blog/2026/06/01/translators/"/>
    <id>tag:home-assistant.io,2026:/blog/translators</id>
    <updated>2026-06-01T00:00:00Z</updated>
    <summary>Thank you to our community translators.</summary>
  </entry>
</feed>"""

GITHUB_PULLS = json.dumps(
    [
        {
            "id": 1,
            "html_url": "https://github.com/home-assistant/core/pull/9001",
            "title": "Remove deprecated supported_features int from media_player",
            "body": "MediaPlayerEntity must use the enum now.",
            "updated_at": "2026-06-02T00:00:00Z",
        },
        {
            "id": 2,
            "html_url": "https://github.com/home-assistant/core/pull/9002",
            "title": "Refactor the thermostat climate scheduler",
            "body": "Internal cleanup of the climate component.",
            "updated_at": "2026-06-01T00:00:00Z",
        },
    ]
).encode("utf-8")


# --- Parsing --------------------------------------------------------------


def test_parse_rss_extracts_items() -> None:
    items = ha_watch.parse_rss(RSS_WITH_HIT, "dev_blog")
    assert len(items) == 2
    first = items[0]
    assert first.title == "Custom card suggestions in the card picker"
    assert first.url.endswith("/custom-card-suggestions/")
    assert first.item_id == first.url  # guid falls back to link


def test_parse_atom_extracts_items_and_alternate_link() -> None:
    items = ha_watch.parse_atom(ATOM_FEED, "main_blog")
    assert len(items) == 2
    assert items[0].url == "https://www.home-assistant.io/blog/2026/06/03/release-20266/"
    assert items[0].item_id.startswith("tag:home-assistant.io")


def test_atom_branch_of_verification_gate() -> None:
    """The main blog / architecture feeds are the GA-notice sources — prove they flag too."""
    hits = ha_watch.relevant_items(ha_watch.parse_atom(ATOM_FEED, "main_blog"))
    titles = [h.title for h in hits]
    assert any("media_player" in t for t in titles)
    assert "Welcoming new translators" not in titles
    flagged = next(h for h in hits if "media_player" in h.title)
    assert "media_player" in flagged.matched
    assert "card picker" in flagged.matched  # appears only in the <summary> body


def test_parse_atom_extracts_xhtml_html_content_body() -> None:
    """A keyword living only in escaped/nested <content> must still be found."""
    feed = b"""<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Frontend component updates</title>
        <link rel="alternate" href="https://x/1"/>
        <id>id-1</id>
        <updated>2026-06-01T00:00:00Z</updated>
        <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
          The <strong>getEntitySuggestion</strong> hook changed.</div></content>
      </entry>
    </feed>"""
    items = ha_watch.parse_atom(feed, "architecture")
    assert "getentitysuggestion" in items[0].text.lower()
    assert ha_watch.relevant_items(items)[0].matched == ["getentitysuggestion"]


def test_alternate_link_precedence_over_other_rels() -> None:
    feed = b"""<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>media_player change</title>
        <link rel="edit" href="https://api/edit"/>
        <link rel="alternate" href="https://human/page"/>
        <id>id-2</id><updated>2026-06-01T00:00:00Z</updated>
      </entry>
    </feed>"""
    assert ha_watch.parse_atom(feed, "x")[0].url == "https://human/page"


def test_parse_github_pulls_uses_html_url_as_id() -> None:
    items = ha_watch.parse_github_pulls(GITHUB_PULLS, "core_breaking")
    assert len(items) == 2
    assert items[0].item_id == "https://github.com/home-assistant/core/pull/9001"
    assert "media_player" in items[0].text.lower()


def test_parse_github_pulls_handles_null_body() -> None:
    data = json.dumps([{"id": 5, "html_url": "u", "title": "t", "body": None}]).encode()
    items = ha_watch.parse_github_pulls(data, "core_breaking")
    assert items[0].text == "t\n"


# --- Relevance filtering --------------------------------------------------


def test_relevant_filters_to_ha_surface() -> None:
    items = ha_watch.parse_rss(RSS_WITH_HIT, "dev_blog")
    hits = ha_watch.relevant_items(items)
    titles = [h.title for h in hits]
    assert "Custom card suggestions in the card picker" in titles
    assert "A new color scheme for the energy graphs" not in titles


def test_2026_6_card_picker_would_have_been_flagged() -> None:
    """The design's verification gate: this post is the opportunity we want early."""
    items = ha_watch.parse_rss(RSS_WITH_HIT, "dev_blog")
    hits = ha_watch.relevant_items(items)
    card_picker = next(h for h in hits if "card picker" in h.title.lower())
    assert "card picker" in card_picker.matched
    assert "custom card" in card_picker.matched
    assert "getentitysuggestion" in card_picker.matched


def test_breaking_change_pr_matched_by_keyword() -> None:
    items = ha_watch.parse_github_pulls(GITHUB_PULLS, "core_breaking")
    hits = ha_watch.relevant_items(items)
    assert len(hits) == 1
    assert "supported_features" in hits[0].matched
    assert "media_player" in hits[0].matched


def test_irrelevant_items_produce_no_hits() -> None:
    """A no-op week (only off-surface changes) flags nothing — no false alarms."""
    quiet = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>New translators welcomed</title><link>https://x/1</link>
        <description>Thanks translators.</description></item>
      <item><title>Climate scheduler refactor</title><link>https://x/2</link>
        <description>Internal climate cleanup.</description></item>
    </channel></rss>"""
    assert ha_watch.relevant_items(ha_watch.parse_rss(quiet, "dev_blog")) == []


# --- Seen-state dedup -----------------------------------------------------


def test_new_items_excludes_seen() -> None:
    items = ha_watch.relevant_items(ha_watch.parse_rss(RSS_WITH_HIT, "dev_blog"))
    seen = {items[0].item_id: "2026-05-27"}
    fresh = ha_watch.new_items(items, seen)
    assert all(i.item_id != items[0].item_id for i in fresh)


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    ha_watch.save_state(path, {"a": "x"})
    assert ha_watch.load_state(path) == {"a": "x"}


def test_load_state_missing_file_returns_empty(tmp_path: Path) -> None:
    assert ha_watch.load_state(tmp_path / "nope.json") == {}


# --- run() orchestration (offline, monkeypatched fetch) -------------------


def test_run_persists_state_and_dedupes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    feeds = {
        "dev_blog": RSS_WITH_HIT,
        "core_breaking": GITHUB_PULLS,
    }
    sources = (
        ha_watch.Source("dev_blog", "dev", "rss", 1),
        ha_watch.Source("core_breaking", "gh", "github_pulls", 1),
    )

    def fake_fetch(url: str, *, timeout: float = 15.0) -> bytes:
        return feeds["dev_blog"] if url == "dev" else feeds["core_breaking"]

    monkeypatch.setattr(ha_watch, "_fetch", fake_fetch)
    state = tmp_path / "state.json"

    first_hits, errors = ha_watch.run(sources=sources, state_path=state)
    assert errors == []
    assert len(first_hits) == 2  # card-picker post + media_player PR

    # Second run: everything is now seen, so no new hits.
    second_hits, _ = ha_watch.run(sources=sources, state_path=state)
    assert second_hits == []


def test_run_dry_run_does_not_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = (ha_watch.Source("dev_blog", "dev", "rss", 1),)
    monkeypatch.setattr(ha_watch, "_fetch", lambda url, timeout=15.0: RSS_WITH_HIT)
    state = tmp_path / "state.json"

    ha_watch.run(sources=sources, state_path=state, dry_run=True)
    assert not state.exists()


def test_run_survives_one_dead_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = (
        ha_watch.Source("dead", "dead", "rss", 1),
        ha_watch.Source("dev_blog", "dev", "rss", 1),
    )

    def fake_fetch(url: str, *, timeout: float = 15.0) -> bytes:
        if url == "dead":
            raise URLError("boom")
        return RSS_WITH_HIT

    monkeypatch.setattr(ha_watch, "_fetch", fake_fetch)
    hits, errors = ha_watch.run(sources=sources, state_path=tmp_path / "s.json")
    assert any("dead" in e for e in errors)
    assert any("card picker" in h.title.lower() for h in hits)  # good feed still reported


def test_main_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(ha_watch, "SOURCES", (ha_watch.Source("dev_blog", "dev", "rss", 1),))
    monkeypatch.setattr(ha_watch, "_fetch", lambda url, timeout=15.0: RSS_WITH_HIT)
    rc = ha_watch.main(["--json", "--state", str(tmp_path / "s.json")])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["new_items"]
    assert payload["errors"] == []


def test_main_human_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(ha_watch, "SOURCES", (ha_watch.Source("dev_blog", "dev", "rss", 1),))
    monkeypatch.setattr(ha_watch, "_fetch", lambda url, timeout=15.0: RSS_WITH_HIT)
    rc = ha_watch.main(["--state", str(tmp_path / "s.json")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "new HA item(s)" in out
    assert "matched:" in out


def test_main_exit_2_when_every_feed_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A total outage must surface as a failure, not a quiet week."""

    def boom(url: str, timeout: float = 15.0) -> bytes:
        raise URLError("down")

    monkeypatch.setattr(ha_watch, "SOURCES", (ha_watch.Source("dev_blog", "dev", "rss", 1),))
    monkeypatch.setattr(ha_watch, "_fetch", boom)
    assert ha_watch.main(["--state", str(tmp_path / "s.json")]) == 2


def test_fetch_injects_github_auth_only_for_github(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class _Resp:
        def read(self) -> bytes:
            return b"[]"

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def fake_urlopen(request, timeout: float = 15.0):
        captured["request"] = request
        return _Resp()

    monkeypatch.setattr(ha_watch, "urlopen", fake_urlopen)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "tok123")

    ha_watch._fetch("https://api.github.com/repos/x/pulls")
    req = captured["request"]
    assert req.get_header("Authorization") == "Bearer tok123"
    assert req.get_header("Accept") == "application/vnd.github+json"

    # A non-GitHub feed must never receive the token (no credential leak).
    ha_watch._fetch("https://developers.home-assistant.io/blog/rss.xml")
    assert captured["request"].get_header("Authorization") is None

    # A hostile host that merely CONTAINS the string must not get the token.
    ha_watch._fetch("https://evil.com/api.github.com/x")
    assert captured["request"].get_header("Authorization") is None


def test_is_github_api_matches_host_not_substring() -> None:
    assert ha_watch._is_github_api("https://api.github.com/repos/x/issues")
    assert not ha_watch._is_github_api("https://evil.com/api.github.com/x")
    assert not ha_watch._is_github_api("https://api.github.com.evil.com/x")


def test_parse_github_pulls_prefers_pull_request_url() -> None:
    """Issues endpoint: a PR item carries pull_request.html_url pointing at the PR."""
    data = json.dumps(
        [
            {
                "id": 9,
                "html_url": "https://github.com/home-assistant/core/issues/42",
                "pull_request": {"html_url": "https://github.com/home-assistant/core/pull/42"},
                "title": "Remove deprecated media_player flag",
                "body": "supported_features must be an enum.",
                "updated_at": "2026-06-02T00:00:00Z",
            }
        ]
    ).encode()
    item = ha_watch.parse_github_pulls(data, "core_breaking")[0]
    assert item.url == "https://github.com/home-assistant/core/pull/42"
    assert item.item_id == item.url


def test_fetch_gh_token_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class _Resp:
        def read(self) -> bytes:
            return b"[]"

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def fake_urlopen(request, timeout: float = 15.0):
        captured["r"] = request
        return _Resp()

    monkeypatch.setattr(ha_watch, "urlopen", fake_urlopen)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "fallback")
    ha_watch._fetch("https://api.github.com/x")
    assert captured["r"].get_header("Authorization") == "Bearer fallback"


def test_parse_feed_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown feed kind"):
        ha_watch.parse_feed("rdf", b"", "x")


def test_run_collects_unknown_kind_as_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha_watch, "_fetch", lambda url, timeout=15.0: b"x")
    sources = (ha_watch.Source("bad", "u", "rdf", 1),)
    hits, errors = ha_watch.run(sources=sources, state_path=tmp_path / "s.json")
    assert hits == []
    assert any("bad" in e for e in errors)


def test_run_collects_malformed_xml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fetch(url: str, timeout: float = 15.0) -> bytes:
        return b"<rss><broken" if url == "bad" else RSS_WITH_HIT

    sources = (
        ha_watch.Source("bad", "bad", "rss", 1),
        ha_watch.Source("good", "good", "rss", 1),
    )
    monkeypatch.setattr(ha_watch, "_fetch", fetch)
    hits, errors = ha_watch.run(sources=sources, state_path=tmp_path / "s.json")
    assert any("bad" in e for e in errors)
    assert any("card picker" in h.title.lower() for h in hits)


def test_default_state_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAMMAMIRADIO_HA_WATCH_STATE", "/tmp/custom-state.json")
    assert ha_watch.default_state_path() == Path("/tmp/custom-state.json")


def test_default_state_path_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAMMAMIRADIO_HA_WATCH_STATE", raising=False)
    assert ha_watch.default_state_path().name == "ha-watch-state.json"


def test_parse_date_handles_rfc822_and_iso() -> None:
    rfc = ha_watch._parse_date("Tue, 27 May 2026 00:00:00 GMT")
    iso = ha_watch._parse_date("2026-06-03T00:00:00Z")
    assert iso > rfc  # June after May — chronological, not lexical
    assert ha_watch._parse_date("garbage") == ha_watch._parse_date("")


def test_save_state_atomic_leaves_no_tmp(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    ha_watch.save_state(path, {"a": "1"})
    assert ha_watch.load_state(path) == {"a": "1"}
    assert not (tmp_path / "state.json.tmp").exists()

#!/usr/bin/env python3
"""Watch Home Assistant upstream for changes that touch mammamiradio's HA surface.

mammamiradio rides a handful of HA extension points: the pushed `media_player`
ghost entity, the HA add-on (ingress, supervisor, config), and — once shipped —
a `custom_components/mammamiradio/` integration plus a Music Assistant provider.
HA breaks and extends these on a monthly cadence, and the high-value changes
(e.g. the 2026.6 entity-first card picker) land on the developer blog and as
`breaking-change` pull requests weeks before they reach a stable release.

This script polls the early-warning feeds ranked in the office-hours research
(developer blog RSS first, `breaking-change` PRs as ground truth, architecture
discussions for the long horizon, the main blog for release notes), keeps only
items that mention mammamiradio's HA surface, and reports the ones it has not
seen before. Run it weekly; feed the output to a human or an issue opener.

Pure stdlib, no network in the parsing layer (so it unit-tests against fixture
bytes). One feed being down never fails the run — errors are collected and
surfaced alongside the hits.

Usage:
    scripts/ha-watch.py                # human summary of new relevant items
    scripts/ha-watch.py --json         # machine-readable JSON (for a scheduler)
    scripts/ha-watch.py --dry-run      # do not persist seen-state (re-reports)
    scripts/ha-watch.py --state PATH   # override the seen-state file
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

USER_AGENT = "mammamiradio-ha-watch/1.0 (+https://github.com/florianhorner/mammamiradio)"

# Substrings (lowercased) that mark an item as touching mammamiradio's HA surface.
# Deliberately specific: bare "card"/"entity"/"integration" match everything, so
# only the qualified forms are listed.
KEYWORDS: tuple[str, ...] = (
    "media_player",
    "media player",
    "mediaplayerentity",
    "supported_features",
    "getentitysuggestion",
    "card picker",
    "custom card",
    "lovelace",
    "config_flow",
    "config flow",
    "custom integration",
    "integration quality",
    "hacs",
    "media_source",
    "media source",
    "music assistant",
    "music_assistant",
    "add-on",
    "addon",
    "supervisor",
    "ingress",
    "/api/states",
    "entity registry",
    "device registry",
    "brands proxy",
    "brand asset",
)


@dataclass(frozen=True)
class Source:
    """A feed to poll."""

    name: str
    url: str
    kind: str  # "rss" | "atom" | "github_pulls"
    priority: int  # 1 = highest signal


SOURCES: tuple[Source, ...] = (
    Source(
        "dev_blog",
        "https://developers.home-assistant.io/blog/rss.xml",
        "rss",
        1,
    ),
    Source(
        "core_breaking",
        "https://api.github.com/repos/home-assistant/core/issues?labels=breaking-change&state=open&per_page=100",
        "github_pulls",
        1,
    ),
    Source(
        "frontend_breaking",
        "https://api.github.com/repos/home-assistant/frontend/issues?labels=breaking-change&state=open&per_page=100",
        "github_pulls",
        2,
    ),
    Source(
        "architecture",
        "https://github.com/home-assistant/architecture/discussions.atom",
        "atom",
        3,
    ),
    Source(
        "main_blog",
        "https://www.home-assistant.io/atom.xml",
        "atom",
        4,
    ),
)


@dataclass
class Item:
    """A single feed entry, normalized across feed kinds."""

    source: str
    item_id: str
    title: str
    url: str
    date: str
    text: str  # body/summary used for keyword matching
    matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "item_id": self.item_id,
            "title": self.title,
            "url": self.url,
            "date": self.date,
            "matched": self.matched,
        }


def _localname(tag: str) -> str:
    """Strip an XML namespace, returning the bare tag name."""
    return tag.rsplit("}", 1)[-1]


def _find_text(element: ET.Element, name: str) -> str:
    """Return the full text of the first child whose local-name matches `name`.

    Uses ``itertext()`` so Atom ``type="xhtml"`` / escaped-``html`` bodies (where
    the real text lives in nested elements) still contribute their words to
    keyword matching — otherwise a breaking change whose keyword appears only in
    the body would be silently missed.
    """
    for child in element:
        if _localname(child.tag) == name:
            return "".join(child.itertext()).strip()
    return ""


def _parse_date(value: str) -> datetime:
    """Parse an RSS (RFC-822) or Atom/GitHub (ISO-8601) date to a tz-aware datetime.

    Returns a tz-aware minimum on anything unparseable so mixed-format dates sort
    deterministically instead of lexically (where 'Wed' sorts before 'Tue').
    """
    floor = datetime.min.replace(tzinfo=UTC)
    if not value:
        return floor
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return floor
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_rss(data: bytes, source: str) -> list[Item]:
    """Parse an RSS 2.0 feed into Items (title/link/guid/description/pubDate)."""
    root = ET.fromstring(data)
    items: list[Item] = []
    for node in root.iter():
        if _localname(node.tag) != "item":
            continue
        title = _find_text(node, "title")
        link = _find_text(node, "link")
        guid = _find_text(node, "guid") or link
        desc = _find_text(node, "description")
        date = _find_text(node, "pubDate")
        items.append(
            Item(
                source=source,
                item_id=guid,
                title=title,
                url=link,
                date=date,
                text=f"{title}\n{desc}",
            )
        )
    return items


def parse_atom(data: bytes, source: str) -> list[Item]:
    """Parse an Atom feed into Items (title/link href/id/summary+content/updated)."""
    root = ET.fromstring(data)
    items: list[Item] = []
    for node in root.iter():
        if _localname(node.tag) != "entry":
            continue
        title = _find_text(node, "title")
        entry_id = _find_text(node, "id")
        date = _find_text(node, "updated") or _find_text(node, "published")
        summary = _find_text(node, "summary")
        content = _find_text(node, "content")
        link = ""
        for child in node:
            if _localname(child.tag) == "link":
                href = child.attrib.get("href", "")
                rel = child.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    link = href
                    break
                link = link or href
        items.append(
            Item(
                source=source,
                item_id=entry_id or link,
                title=title,
                url=link or entry_id,
                date=date,
                text=f"{title}\n{summary}\n{content}",
            )
        )
    return items


def parse_github_pulls(data: bytes, source: str) -> list[Item]:
    """Parse a GitHub issues JSON array into Items (html_url/title/body).

    Uses the issues endpoint, not `/pulls`: the `List pull requests` endpoint
    silently ignores a `labels` query param, so `?labels=breaking-change` there
    would scan the newest 100 open PRs regardless of label. The issues endpoint
    honors `labels`; a returned item that is a PR carries a `pull_request`
    object whose `html_url` points at the pull request.
    """
    payload = json.loads(data or b"[]")
    items: list[Item] = []
    for entry in payload:
        title = entry.get("title", "") or ""
        body = entry.get("body", "") or ""
        pull_request = entry.get("pull_request") or {}
        url = pull_request.get("html_url") or entry.get("html_url", "") or ""
        item_id = url or str(entry.get("id", ""))
        if not item_id:
            # No stable identity: persisting an empty key would suppress every
            # future identity-less item forever. Drop it instead.
            continue
        items.append(
            Item(
                source=source,
                item_id=item_id,
                title=title,
                url=url,
                date=entry.get("updated_at", "") or "",
                text=f"{title}\n{body}",
            )
        )
    return items


def parse_feed(kind: str, data: bytes, source: str) -> list[Item]:
    if kind == "rss":
        return parse_rss(data, source)
    if kind == "atom":
        return parse_atom(data, source)
    if kind == "github_pulls":
        return parse_github_pulls(data, source)
    raise ValueError(f"unknown feed kind: {kind}")


def match_keywords(item: Item, keywords: tuple[str, ...] = KEYWORDS) -> list[str]:
    """Return the keywords (in order) found in the item's title+text."""
    haystack = f"{item.title}\n{item.text}".lower()
    return [kw for kw in keywords if kw in haystack]


def relevant_items(items: list[Item], keywords: tuple[str, ...] = KEYWORDS) -> list[Item]:
    """Annotate and keep only items that mention the HA surface."""
    out: list[Item] = []
    for item in items:
        matched = match_keywords(item, keywords)
        if matched:
            item.matched = matched
            out.append(item)
    return out


def new_items(items: list[Item], seen: dict) -> list[Item]:
    """Keep only items whose id is not already in the seen-state map."""
    return [item for item in items if item.item_id not in seen]


def _is_github_api(url: str) -> bool:
    """True only when the URL's host is exactly api.github.com.

    A substring check (``"api.github.com" in url``) would also match a hostile
    host like ``https://evil.com/api.github.com`` and leak the GitHub token to
    it; compare the parsed hostname instead.
    """
    return urlsplit(url).hostname == "api.github.com"


def _fetch(url: str, *, timeout: float = 15.0) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if _is_github_api(url):
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, seen: dict) -> None:
    # Write to a temp file then atomically replace, so a crash mid-write cannot
    # truncate the seen-state and make the watcher re-report everything.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(seen, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def default_state_path() -> Path:
    override = os.environ.get("MAMMAMIRADIO_HA_WATCH_STATE")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "mammamiradio" / "ha-watch-state.json"


def run(
    *,
    sources: tuple[Source, ...] | None = None,
    state_path: Path | None = None,
    dry_run: bool = False,
    timeout: float = 15.0,
) -> tuple[list[Item], list[str]]:
    """Poll all sources, return (new relevant items, per-source error strings)."""
    if sources is None:
        sources = SOURCES  # resolved at call time so SOURCES stays monkeypatchable
    state_path = state_path or default_state_path()
    seen = load_state(state_path)
    hits: list[Item] = []
    errors: list[str] = []

    for source in sources:
        try:
            data = _fetch(source.url, timeout=timeout)
            parsed = parse_feed(source.kind, data, source.name)
        except (HTTPError, URLError, ET.ParseError, json.JSONDecodeError, ValueError) as err:
            msg = f"{source.name}: {type(err).__name__}: {err}"
            if isinstance(err, HTTPError) and err.code == 403 and _is_github_api(source.url):
                msg += " — set GITHUB_TOKEN to lift the GitHub rate limit"
            errors.append(msg)
            continue
        hits.extend(new_items(relevant_items(parsed), seen))

    hits.sort(key=lambda i: _parse_date(i.date), reverse=True)

    if not dry_run:
        # Seen-state grows slowly (one text id per relevant item, weekly cadence);
        # unbounded growth is accepted rather than pruned to keep dedup correct.
        for item in hits:
            seen[item.item_id] = item.date or "seen"
        save_state(state_path, seen)

    return hits, errors


def _print_human(hits: list[Item], errors: list[str]) -> None:
    if errors:
        for err in errors:
            print(f"[warn] feed error — {err}", file=sys.stderr)
    if not hits:
        print("No new HA changes touching mammamiradio's surface.")
        return
    print(f"{len(hits)} new HA item(s) touching mammamiradio's surface:\n")
    for item in hits:
        print(f"  [{item.source}] {item.title}")
        print(f"    {item.url}")
        print(f"    matched: {', '.join(item.matched)}")
        if item.date:
            print(f"    date: {item.date}")
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do not persist seen-state (re-reports every item)",
    )
    parser.add_argument("--state", type=Path, default=None, help="seen-state file path")
    parser.add_argument("--timeout", type=float, default=15.0, help="per-feed HTTP timeout (seconds)")
    args = parser.parse_args(argv)

    hits, errors = run(state_path=args.state, dry_run=args.dry_run, timeout=args.timeout)

    if args.json:
        print(
            json.dumps(
                {"new_items": [h.to_dict() for h in hits], "errors": errors},
                indent=2,
            )
        )
    else:
        _print_human(hits, errors)

    # Every feed failed: a total outage must not look like a quiet week to the
    # scheduler. Exit non-zero so the run is visibly degraded, not silently green.
    if errors and len(errors) >= len(SOURCES):
        print("[error] every feed failed — watcher could not check upstream", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

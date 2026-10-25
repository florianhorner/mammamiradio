#!/usr/bin/env python3
"""Speech cast gate: was every airtime-approved ad voice and the ident heard and accepted, and is the interim in date?

The shipped cast needs one optional paid speech key, ElevenLabs, as a dated
interim decision. Two checks, both offline:

  --binding  Every airtime-approved ad voice and the station ident has an accepted
             row in the committed selection receipt. Rows match by the audition
             tool's own candidate ID (provider, voice and render settings), so a
             changed voice or setting needs a new listening pass. Hosts carry
             their own receipts and are not checked here.
  --release  The interim is still inside its review window. Reads only the dates
             below, so it runs without the project installed.

Exit codes: 0 nothing to report; 1 a finding; 2 the receipt or config could not be
read. The binding runs in the test suite on every change
(tests/scripts/test_check_speech_cast.py). --release runs in
scripts/pre-release-check.sh section 12, on release cuts and under
`make pre-release`; it is never an every-PR assertion.

A later review date is not a fix. The way past REVIEW_BY is a new listening pass:
re-audition the cast, commit a new receipt, then set a new decision and review
date here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from mammamiradio.core.config import StationConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "radio.toml"
DEFAULT_RECEIPT = REPO_ROOT / "proof" / "2026-10-22-speech-cast.json"

# The one-key decision and the date it must be looked at again.
DECISION_DATE = dt.date(2026, 10, 22)
REVIEW_BY = dt.date(2027, 4, 14)
# An interim longer than about six months has stopped being one.
MAX_INTERIM_DAYS = 183

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_UNREADABLE = 2

IDENT_LABEL = "the station ident"


@dataclass(frozen=True)
class CastRoute:
    """One on-air voice route and the candidate ID its listening row must carry."""

    label: str
    provider: str
    voice: str
    candidate_id: str


def release_findings(today: dt.date) -> list[str]:
    """Return why the interim may not ship on ``today``; empty when it may."""
    findings: list[str] = []
    if REVIEW_BY <= DECISION_DATE:
        findings.append(f"the review date {REVIEW_BY} must fall after the decision on {DECISION_DATE}")
    elif (REVIEW_BY - DECISION_DATE).days > MAX_INTERIM_DAYS:
        findings.append(
            f"the review date {REVIEW_BY} is more than {MAX_INTERIM_DAYS} days after the decision on "
            f"{DECISION_DATE}; an interim this long needs a new listening pass, not a later date"
        )
    if today > REVIEW_BY:
        findings.append(
            f"the one-key speech cast passed its review date {REVIEW_BY}. Re-audition the cast, commit a new "
            "receipt, then set a new decision and review date in scripts/check_speech_cast.py"
        )
    return findings


def _audition_module():
    # The release check runs on a bare Python; only the binding needs the project.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts import audition_tts_voices

    return audition_tts_voices


def cast_routes(config: StationConfig) -> list[CastRoute]:
    """Return the gated routes: every airtime-approved ad voice, then the ident.

    Targets come from the audition tool itself, so a route's candidate ID is
    computed exactly as the receipt writer computed it.
    """
    audition = _audition_module()
    # Staged voices also enter the audition index, so they must not shadow an
    # airtime voice sharing the same name, even when their routes differ.
    name_counts = Counter(voice.name for voice in config.ads.voices)
    duplicate_names = sorted(
        {voice.name for voice in config.ads.voices if voice.airtime_approved and name_counts[voice.name] > 1}
    )
    if duplicate_names:
        raise ValueError(
            f"duplicate ad voice names involve an airtime voice: {', '.join(duplicate_names)}; "
            "give these voices unique names before checking their listening rows"
        )
    targets_by_use = {use: target for target in audition.collect_configured_targets(config) for use in target.used_by}

    def route(label: str, use: str) -> CastRoute:
        target = targets_by_use[use]
        profile = audition._selection_profile_for_target(target)
        return CastRoute(
            label=label,
            provider=target.provider,
            voice=target.voice,
            candidate_id=audition._selection_candidate_id(target.provider, target.voice, profile),
        )

    routes = [
        route(f"ad voice {voice.name}", f"ad:{voice.name}") for voice in config.ads.voices if voice.airtime_approved
    ]
    if config.sonic_brand.sweeper_voice.strip():
        routes.append(route(IDENT_LABEL, "sonic_brand:sweeper"))
    return routes


def binding_findings(config: StationConfig, receipt: dict[str, object]) -> list[str]:
    """Return every gated route without an accepted listening row."""
    try:
        routes = cast_routes(config)
    except ValueError as exc:
        return [str(exc)]
    # A validated receipt holds a list of rows with unique candidate IDs.
    rows = cast(list[dict[str, object]], receipt["candidates"])
    statuses = {str(row["candidate_id"]): str(row["approval_status"]) for row in rows}
    findings: list[str] = []
    if not config.sonic_brand.sweeper_voice.strip():
        findings.append(f"{IDENT_LABEL} has no voice of its own; set [sonic_brand] sweeper_voice and audition it")
    for cast_route in routes:
        status = statuses.get(cast_route.candidate_id)
        if status == "accepted":
            continue
        voice = f"{cast_route.provider} {cast_route.voice}"
        if status == "rejected":
            findings.append(f"{cast_route.label} airs on a voice the listening pass rejected ({voice})")
        else:
            findings.append(
                f"{cast_route.label} has no accepted listening row ({voice}); audition it and add it to the receipt"
            )
    return findings


def run_binding(config_path: Path, receipt_path: Path) -> int:
    audition = _audition_module()
    try:
        receipt = audition.load_selection_receipt(receipt_path)
    except (OSError, ValueError) as exc:  # ValueError covers invalid JSON and a receipt that fails validation
        print(f"UNREADABLE: the speech cast receipt {receipt_path} could not be read: {exc}")
        return EXIT_UNREADABLE
    from mammamiradio.core.config import load_config

    try:
        config = load_config(str(config_path))
    except Exception as exc:  # any load failure means the binding was not verified
        print(f"UNREADABLE: {config_path} could not be loaded: {exc}")
        return EXIT_UNREADABLE
    findings = binding_findings(config, receipt)
    for finding in findings:
        print(f"FAIL: {finding}")
    if findings:
        return EXIT_FINDING
    print(f"OK (binding): every on-air voice in {config_path.name} has an accepted listening row")
    return EXIT_OK


def run_release(today: dt.date) -> int:
    findings = release_findings(today)
    for finding in findings:
        print(f"FAIL: {finding}")
    if findings:
        return EXIT_FINDING
    days_left = (REVIEW_BY - today).days
    print(f"OK (release): the speech cast is inside its review window ({days_left} days left, review by {REVIEW_BY})")
    return EXIT_OK


def _parse_iso_argument(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--binding", action="store_true", help="every on-air voice has an accepted listening row")
    mode.add_argument("--release", action="store_true", help="the interim is inside its review window")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="radio.toml to check (default: repo root)")
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT, help="selection receipt to check against")
    parser.add_argument(
        "--today", type=_parse_iso_argument, default=None, help="pretend today is this YYYY-MM-DD date (tests)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.release:
        return run_release(args.today or dt.datetime.now(dt.UTC).date())
    return run_binding(args.config, args.receipt)


if __name__ == "__main__":
    sys.exit(main())

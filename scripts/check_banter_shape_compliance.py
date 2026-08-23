#!/usr/bin/env python3
"""Operator-only checker for *observed* banter skeletons.

Classifies who opens, whether a cutoff occurs, and how the exchange lands.
It does not score selected ``exchange_shape_id`` values — those are prompt
intent, not the aired skeleton. Not wired into ``make check``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from mammamiradio.hosts.scriptwriter import _banter_line_needs_immediate_reply

_HOST_ALIASES = {
    "marco": "marco",
    "giulia": "giulia",
}
_SPEAKER_LINE_RE = re.compile(r"^\s*([^:]{1,40})\s*:\s*(.*)$")
_KNOWN_SKIP_REASONS = frozenset(
    {
        "chaos",
        "festival",
        "guest",
        "ineligible_roster",
        "listener_truth_repair",
        "script_fallback",
        "single_host_result",
    }
)


def _normalize_host(raw: str) -> str:
    token = raw.strip().casefold()
    for needle, canonical in _HOST_ALIASES.items():
        if needle in token:
            return canonical
    return token or "unknown"


def parse_line(item: Any) -> tuple[str, str]:
    """Accept ``{host, text}``, a ``Host: text`` string, or a 2-tuple."""

    if isinstance(item, Mapping):
        host = str(item.get("host") or item.get("speaker") or "")
        text = str(item.get("text") or item.get("line") or "")
        return _normalize_host(host), text.strip()
    if isinstance(item, str):
        match = _SPEAKER_LINE_RE.match(item)
        if match:
            return _normalize_host(match.group(1)), match.group(2).strip()
        return "unknown", item.strip()
    if isinstance(item, Sequence) and len(item) >= 2:
        return _normalize_host(str(item[0])), str(item[1]).strip()
    return "unknown", str(item).strip()


def classify_skeleton(lines: Iterable[Any]) -> dict[str, str]:
    """Classify one aired exchange from host-attributed lines."""

    parsed = [parse_line(item) for item in lines]
    spoken = [(host, text) for host, text in parsed if text]
    if not spoken:
        return {
            "opener": "unknown",
            "cutoff": "none",
            "cutoff_direction": "none",
            "landing": "empty",
            "skeleton_id": "empty",
        }

    opener = spoken[0][0]
    cutoff_directions = [
        f"{spoken[index + 1][0]}_cuts_{host}"
        for index, (host, text) in enumerate(spoken[:-1])
        if _banter_line_needs_immediate_reply(text)
    ]
    last_text = spoken[-1][1]
    landing = "open" if _banter_line_needs_immediate_reply(last_text) else "clean"
    cutoff_tag = "cutoff" if cutoff_directions else "through"
    cutoff_direction = "+".join(cutoff_directions) if cutoff_directions else "through"
    return {
        "opener": opener,
        "cutoff": cutoff_tag,
        "cutoff_direction": cutoff_direction,
        "landing": landing,
        "skeleton_id": f"{opener}|{cutoff_direction}|{landing}",
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_no}: invalid JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise SystemExit(f"{path}:{line_no}: expected an object")
        rows.append(row)
    return rows


def _lines_from_row(row: Mapping[str, Any], *, provenance: bool) -> list[Any]:
    if isinstance(row.get("exchange_lines"), list):
        return row["exchange_lines"]
    if provenance:
        # Tier-2 final_script is bare spoken text and also includes the
        # transition. It cannot reconstruct the shaped speakers honestly.
        return []
    if isinstance(row.get("lines"), list):
        return row["lines"]
    script = row.get("final_script")
    if isinstance(script, list):
        return script
    return []


def _select_evidence(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], Counter[str], list[str], bool]:
    """Select actually-aired, shape-bearing banter from a mixed ledger.

    Hand-curated inputs without ``record`` fields remain supported for operator
    experiments. A real provenance JSONL is joined Tier 2 -> Tier 3 by
    ``segment_id`` and excludes non-banter, unaired, and explicitly skipped rows.
    Ambiguous provenance fails closed instead of silently becoming a skeleton.
    """

    if not any(isinstance(row.get("record"), str) for row in rows):
        return list(rows), Counter(), [], False

    errors: list[str] = []
    excluded: Counter[str] = Counter()
    prepared_by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if row.get("record") != "segment_prepared" or row.get("role") != "banter":
            continue
        segment_id = row.get("segment_id")
        if not isinstance(segment_id, str) or not segment_id:
            errors.append("banter segment_prepared row has no segment_id")
            continue
        if segment_id in prepared_by_id:
            errors.append(f"duplicate segment_prepared id: {segment_id}")
            continue
        prepared_by_id[segment_id] = row

    eligible: list[Mapping[str, Any]] = []
    aired_ids: set[str] = set()
    for row in rows:
        if not (
            row.get("record") == "stream_result"
            and row.get("segment_type") == "banter"
            and row.get("aired_status") == "aired"
        ):
            continue
        segment_id = row.get("segment_id")
        # Canned/fallback audio deliberately has no Tier-2 join id.
        if not isinstance(segment_id, str) or not segment_id:
            continue
        if segment_id in aired_ids:
            errors.append(f"duplicate aired stream_result id: {segment_id}")
            continue
        aired_ids.add(segment_id)
        prepared = prepared_by_id.get(segment_id)
        if prepared is None:
            errors.append(f"aired banter has no segment_prepared row: {segment_id}")
            continue
        shape_id = prepared.get("exchange_shape_id")
        skip_reason = prepared.get("exchange_shape_skip_reason")
        if isinstance(shape_id, str) and shape_id:
            eligible.append(prepared)
            continue
        if isinstance(skip_reason, str) and skip_reason:
            if skip_reason not in _KNOWN_SKIP_REASONS:
                errors.append(f"unknown exchange_shape_skip_reason for {segment_id}: {skip_reason}")
            else:
                excluded[skip_reason] += 1
            continue
        errors.append(f"aired generated banter has neither shape nor skip reason: {segment_id}")

    return eligible, excluded, errors, True


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    skeletons: Counter[str] = Counter()
    openers: Counter[str] = Counter()
    classified: list[dict[str, Any]] = []
    eligible_rows, excluded, evidence_errors, provenance = _select_evidence(rows)
    unclassifiable = 0
    for row in eligible_rows:
        lines = _lines_from_row(row, provenance=provenance)
        parsed = [parse_line(item) for item in lines]
        speakers = {host for host, text in parsed if text and host != "unknown"}
        observed = classify_skeleton(lines)
        if observed["opener"] == "unknown" or observed["skeleton_id"] == "empty" or len(speakers) < 2:
            unclassifiable += 1
            continue
        skeletons[observed["skeleton_id"]] += 1
        openers[observed["opener"]] += 1
        classified.append(
            {
                **observed,
                "segment_id": row.get("segment_id"),
                "selected_shape_id": row.get("exchange_shape_id"),
            }
        )
    cutoff_count = sum(1 for item in classified if item["cutoff"] == "cutoff")
    return {
        "eligible_segments": len(eligible_rows),
        "excluded_segments": sum(excluded.values()),
        "excluded_by_reason": dict(excluded),
        "segments": len(classified),
        "unclassifiable_segments": unclassifiable,
        "evidence_errors": evidence_errors,
        "distinct_skeletons": len(skeletons),
        "skeletons": dict(skeletons),
        "openers": dict(openers),
        "cutoff_rate": (cutoff_count / len(classified)) if classified else 0.0,
        "rows": classified,
    }


def _print_report(summary: Mapping[str, Any]) -> None:
    print(f"eligible aired segments: {summary['eligible_segments']}")
    print(f"excluded aired segments: {summary['excluded_segments']}")
    print(f"classified segments: {summary['segments']}")
    print(f"unclassifiable segments: {summary['unclassifiable_segments']}")
    print(f"distinct observed skeletons: {summary['distinct_skeletons']}")
    print(f"cutoff rate: {summary['cutoff_rate']:.2f}")
    print("openers:")
    for host, count in sorted(summary["openers"].items()):
        print(f"  {host}: {count}")
    print("exclusions:")
    for reason, count in sorted(summary["excluded_by_reason"].items()):
        print(f"  {reason}: {count}")
    print("skeletons:")
    for skeleton_id, count in sorted(summary["skeletons"].items()):
        print(f"  {skeleton_id}: {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "jsonl",
        type=Path,
        help="mixed provenance JSONL, or curated aired banter rows with host-attributed lines",
    )
    parser.add_argument(
        "--fail-below-distinct",
        type=int,
        default=0,
        help="optional operator floor on distinct observed skeletons (not selected ids)",
    )
    args = parser.parse_args(argv)
    if not args.jsonl.is_file():
        print(f"missing file: {args.jsonl}", file=sys.stderr)
        return 2
    summary = summarize(load_jsonl(args.jsonl))
    _print_report(summary)
    if summary["evidence_errors"]:
        for error in summary["evidence_errors"]:
            print(f"invalid evidence: {error}", file=sys.stderr)
        return 2
    if summary["unclassifiable_segments"]:
        print(
            f"{summary['unclassifiable_segments']} eligible aired segment(s) lack host-attributed exchange lines",
            file=sys.stderr,
        )
        return 2
    if args.fail_below_distinct and summary["distinct_skeletons"] < args.fail_below_distinct:
        print(
            f"observed skeleton variety {summary['distinct_skeletons']} < {args.fail_below_distinct}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

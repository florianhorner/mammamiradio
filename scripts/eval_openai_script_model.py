#!/usr/bin/env python3
"""Evaluate OpenAI fallback script models with deterministic integrity receipts.

The command deliberately forces the OpenAI fallback branch in
``scriptwriter._generate_json_response``. It is an online, paid operator tool,
not a CI gate and not evidence of Anthropic-first live output. Use ``--dry-run``
to validate the corpus and preview call/cost bounds without network activity.

Usage:
    python scripts/eval_openai_script_model.py --dry-run
    OPENAI_API_KEY=sk-... python scripts/eval_openai_script_model.py
    OPENAI_API_KEY=sk-... python scripts/eval_openai_script_model.py --models model-a model-b
    OPENAI_API_KEY=sk-... python scripts/eval_openai_script_model.py --fixtures path/to/prompts.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mammamiradio.core.config import load_config  # noqa: E402
from mammamiradio.core.models import StationState, Track  # noqa: E402
from mammamiradio.hosts.scriptwriter import (  # noqa: E402
    _ANTHROPIC_MAX_TOKENS_ESCALATION_FACTOR,
    _OPENAI_REASONING_HEADROOM,
    _generate_json_response,
)
from mammamiradio.hosts.segment_floor import check_floor  # noqa: E402

DEFAULT_FIXTURES = REPO_ROOT / "scripts" / "eval_fixtures" / "openai_script_prompts.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "tmp" / "evals"
RECEIPT_SCHEMA_VERSION = 1
SUPPORTED_CALLERS = frozenset({"banter", "ad", "news_flash", "transition", "memory_extract", "direction"})


def default_models() -> list[str]:
    """Return the OpenAI candidates declared by the canonical model registry."""
    config = load_config(str(REPO_ROOT / "radio.toml"))
    return config.models.default_openai_eval_models()


def estimate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    config,
) -> tuple[float, bool]:
    """Estimate a call using registry pricing and flag unpriced candidates."""
    input_rate, output_rate, unpriced = config.models.price_for_model(model)
    return (
        prompt_tokens * input_rate + completion_tokens * output_rate,
        unpriced,
    )


def load_fixtures(path: Path) -> list[dict[str, Any]]:
    """Load and validate the operator corpus before any provider call."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read fixtures: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"fixtures are not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})") from exc

    if not isinstance(raw, list) or not raw:
        raise ValueError("fixtures must contain a non-empty JSON list")

    fixtures: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, fixture in enumerate(raw, start=1):
        if not isinstance(fixture, dict):
            raise ValueError(f"fixture #{index} must be an object")
        fixture_id = fixture.get("id")
        caller = fixture.get("caller")
        max_tokens = fixture.get("max_tokens")
        prompt = fixture.get("prompt")
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            raise ValueError(f"fixture #{index} needs a non-empty string id")
        if fixture_id in seen_ids:
            raise ValueError(f"fixture id {fixture_id!r} is duplicated")
        if not isinstance(caller, str) or caller not in SUPPORTED_CALLERS:
            allowed = ", ".join(sorted(SUPPORTED_CALLERS))
            raise ValueError(f"fixture {fixture_id!r} has unsupported caller {caller!r}; expected one of: {allowed}")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError(f"fixture {fixture_id!r} needs a positive integer max_tokens")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"fixture {fixture_id!r} needs a non-empty string prompt")
        seen_ids.add(fixture_id)
        fixtures.append(fixture)
    return fixtures


def validate_models(models: list[str]) -> list[str]:
    """Return non-empty model identifiers or a concise preflight error."""
    cleaned = [model.strip() for model in models if isinstance(model, str) and model.strip()]
    if not cleaned or len(cleaned) != len(models):
        raise ValueError("models must contain one or more non-empty model IDs")
    return cleaned


def preflight(models: list[str], fixtures: list[dict[str, Any]], *, config) -> dict[str, Any]:
    """Build a no-network run preview from fixture bounds and registry prices."""
    logical_cases = len(fixtures) * len(models)
    base_completion_tokens_per_model = sum(fixture["max_tokens"] + _OPENAI_REASONING_HEADROOM for fixture in fixtures)
    retry_completion_tokens_per_model = sum(
        round(fixture["max_tokens"] * _ANTHROPIC_MAX_TOKENS_ESCALATION_FACTOR) + _OPENAI_REASONING_HEADROOM
        for fixture in fixtures
    )
    # `_generate_json_response` makes one base OpenAI call and may make one
    # escalated retry. Each logical call can make a second HTTP attempt when a
    # non-reasoning model rejects ``reasoning_effort``. Report that absolute
    # request ceiling separately from the two successful completion attempts
    # used for the completion-token cost bound below.
    max_completion_tokens_per_model = base_completion_tokens_per_model + retry_completion_tokens_per_model
    model_costs = []
    for model in models:
        max_cost_usd, unpriced = estimate_cost_usd(model, 0, max_completion_tokens_per_model, config=config)
        model_costs.append({"model": model, "max_completion_cost_usd": max_cost_usd, "unpriced": unpriced})
    return {
        "fixture_count": len(fixtures),
        "model_count": len(models),
        "logical_case_count": logical_cases,
        "max_provider_request_count": logical_cases * 4,
        "max_completion_tokens_per_model": max_completion_tokens_per_model,
        "max_completion_tokens_total": max_completion_tokens_per_model * len(models),
        "models": model_costs,
        "max_completion_cost_usd": sum(item["max_completion_cost_usd"] for item in model_costs),
    }


def format_preflight(details: dict[str, Any]) -> str:
    """Format a legible no-network preview for terminal operators."""
    lines = [
        "EVAL PREFLIGHT (no provider calls)",
        f"fixtures: {details['fixture_count']}",
        f"models: {details['model_count']}",
        f"logical cases: {details['logical_case_count']}",
        f"maximum provider requests: {details['max_provider_request_count']} "
        "(absolute HTTP-attempt ceiling; a non-reasoning model can retry both the base and escalated calls "
        "without reasoning_effort)",
        f"maximum completion tokens (base + one retry per case): {details['max_completion_tokens_total']}",
        f"max completion cost: ${details['max_completion_cost_usd']:.4f} "
        "(completion only — input/prompt tokens bill on top and are not estimated offline)",
        "candidates:",
    ]
    for model in details["models"]:
        pricing = "UNPRICED — requires --allow-unpriced" if model["unpriced"] else "priced"
        lines.append(f"  {model['model']}: ${model['max_completion_cost_usd']:.4f} max completion, {pricing}")
    return "\n".join(lines)


async def run_one(*, model: str, fixture: dict[str, Any], config, state, run_id: str) -> dict[str, Any]:
    # Force the OpenAI branch to use the model under test regardless of which
    # role the fixture's caller maps to: point every OpenAI catalog entry at it.
    config.models.catalog["openai"] = {"large": model, "small": model}
    record: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "run_id": run_id,
        "model": model,
        "fixture_id": fixture["id"],
        "caller": fixture["caller"],
        "max_tokens": fixture["max_tokens"],
        "json_ok": False,
        "result_status": "generation_error",
        "floor": None,
        "latency_ms": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "output_chars": 0,
        "output_text": None,
        "error": None,
    }
    t_start = time.perf_counter()
    try:
        result = await _generate_json_response(
            prompt=fixture["prompt"],
            config=config,
            state=state,
            model="unused-anthropic-model",  # anthropic_api_key empty → branch skipped
            max_tokens=fixture["max_tokens"],
            caller=fixture["caller"],
        )
        record["latency_ms"] = int((time.perf_counter() - t_start) * 1000)
        record["json_ok"] = True
        record["result_status"] = "evaluated"
        record["floor"] = check_floor(fixture["caller"], result, config).to_dict()
        text = json.dumps(result, ensure_ascii=False)
        record["output_text"] = text
        record["output_chars"] = len(text)
    except Exception as exc:
        record["latency_ms"] = int((time.perf_counter() - t_start) * 1000)
        record["error"] = f"{type(exc).__name__}: {exc}"

    # Token counts come from state deltas. Snapshot before/after each call.
    record["prompt_tokens"] = state._eval_last_prompt_tokens
    record["completion_tokens"] = state._eval_last_completion_tokens
    record["cost_usd"], record["cost_unpriced"] = estimate_cost_usd(
        model,
        record["prompt_tokens"],
        record["completion_tokens"],
        config=config,
    )
    return record


async def run_all(
    models: list[str],
    fixtures: list[dict[str, Any]],
    *,
    config=None,
    api_key: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Run every model/fixture pair after main has completed preflight."""
    if config is None:
        config = load_config(str(REPO_ROOT / "radio.toml"))
    api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY must be set before running the evaluator")
    config.anthropic_api_key = ""  # force OpenAI branch
    config.openai_api_key = api_key
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")

    records: list[dict[str, Any]] = []
    for model in models:
        print(f"\n=== Running model: {model} ===", file=sys.stderr)
        for fixture in fixtures:
            state = StationState(playlist=[Track(title="Eval", artist="Eval", duration_ms=1000, spotify_id="eval")])
            # Snapshot token counters via wrapper attrs so run_one can read deltas.
            before_in = state.api_input_tokens
            before_out = state.api_output_tokens
            wrapped = _TokenSnapshot(state, before_in, before_out)
            record = await run_one(model=model, fixture=fixture, config=config, state=wrapped, run_id=run_id)
            floor_status = record["floor"]["status"] if record["floor"] else "ERROR"
            print(
                f"  {fixture['id']:32s} caller={fixture['caller']:14s} "
                f"json_ok={record['json_ok']!s:5s} floor={floor_status:5s} "
                f"latency={record['latency_ms']}ms tokens={record['prompt_tokens']}/{record['completion_tokens']}"
                + (f" error={record['error']}" if record["error"] else ""),
                file=sys.stderr,
            )
            records.append(record)
    return records


class _TokenSnapshot:
    """Lightweight wrapper that exposes per-call token deltas after the LLM call."""

    def __init__(self, inner: StationState, before_in: int, before_out: int) -> None:
        self._inner = inner
        self._before_in = before_in
        self._before_out = before_out

    def __getattr__(self, name: str):
        if name == "_eval_last_prompt_tokens":
            return self._inner.api_input_tokens - self._before_in
        if name == "_eval_last_completion_tokens":
            return self._inner.api_output_tokens - self._before_out
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value) -> None:
        if name in ("_inner", "_before_in", "_before_out"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)


def _floor_reason_codes(record: dict[str, Any]) -> str:
    floor = record.get("floor")
    if not isinstance(floor, dict):
        return record.get("result_status", "generation_error")
    gates = floor.get("gates")
    if not isinstance(gates, dict):
        return "unknown_floor_shape"
    reasons: list[str] = []
    for gate in gates.values():
        if not isinstance(gate, dict) or gate.get("status") != "FAIL":
            continue
        reason = gate.get("reason")
        if isinstance(reason, str) and reason:
            reasons.append(reason)
    return ",".join(reasons) or "-"


def _floor_status(record: dict[str, Any]) -> str | None:
    floor = record.get("floor")
    return floor.get("status") if isinstance(floor, dict) else None


def summarize(records: list[dict[str, Any]]) -> str:
    """Return a model comparison plus floor failures separate from provider errors."""
    by_model: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_model.setdefault(record["model"], []).append(record)

    lines = ["", "=" * 118, "SUMMARY", "=" * 118]
    header = (
        f"{'model':16s} {'n':>3s} {'json_ok':>8s} {'floor P/F/N':>11s} {'p50_ms':>8s} {'p95_ms':>8s} "
        f"{'mean_chars':>10s} {'in_tok':>7s} {'out_tok':>8s} {'cost_usd':>10s}"
    )
    lines.extend([header, "-" * 118])
    for model, recs in by_model.items():
        n = len(recs)
        ok = sum(1 for record in recs if record["json_ok"])
        floor_counts = {
            status: sum(1 for record in recs if _floor_status(record) == status) for status in ("PASS", "FAIL", "N/A")
        }
        latencies = sorted(record["latency_ms"] for record in recs if record["latency_ms"] is not None)
        p50 = statistics.median(latencies) if latencies else 0
        p95 = latencies[min(len(latencies) - 1, math.ceil(0.95 * (len(latencies) - 1)))] if latencies else 0
        char_records = [record["output_chars"] for record in recs if record["output_chars"]]
        mean_chars = statistics.mean(char_records) if char_records else 0
        in_tok = sum(record["prompt_tokens"] for record in recs)
        out_tok = sum(record["completion_tokens"] for record in recs)
        cost = sum(record["cost_usd"] for record in recs)
        floor_cell = f"{floor_counts['PASS']}/{floor_counts['FAIL']}/{floor_counts['N/A']}"
        lines.append(
            f"{model:16s} {n:3d} {f'{ok}/{n}':>8s} {floor_cell:>11s} {int(p50):8d} {int(p95):8d} "
            f"{int(mean_chars):10d} {in_tok:7d} {out_tok:8d} ${cost:9.4f}"
        )

    floor_failures = [record for record in records if _floor_status(record) == "FAIL"]
    generation_errors = [record for record in records if not record["json_ok"]]
    if floor_failures:
        lines.append("Floor failures:")
        lines.extend(
            f"  {record['model']} {record['fixture_id']}: {_floor_reason_codes(record)}" for record in floor_failures
        )
    if generation_errors:
        lines.append("Provider/JSON failures:")
        lines.extend(
            f"  {record['model']} {record['fixture_id']}: {record['error'] or record['result_status']}"
            for record in generation_errors
        )
    lines.append("=" * 118)
    return "\n".join(lines)


def _preflight_error(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 2


def _mkdir_private(path: Path) -> None:
    """Create missing output directories with private permissions."""
    if path.exists():
        if not path.is_dir():
            raise FileExistsError(f"{path} exists and is not a directory")
        return

    parent = path.parent
    if parent != path:
        _mkdir_private(parent)

    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise
    else:
        path.chmod(0o700)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--models",
        nargs="+",
        help="OpenAI model IDs to evaluate (default: OpenAI catalog entries in model_registry.toml)",
    )
    ap.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, help="Prompt corpus JSON path")
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Where to write the JSONL log")
    ap.add_argument(
        "--dry-run", action="store_true", help="Validate inputs and preview calls/costs without provider calls"
    )
    ap.add_argument(
        "--allow-unpriced",
        action="store_true",
        help="Allow a real run with explicit models absent from registry pricing",
    )
    args = ap.parse_args(argv)

    try:
        fixtures = load_fixtures(args.fixtures)
        config = load_config(str(REPO_ROOT / "radio.toml"))
        models = validate_models(args.models or default_models())
        details = preflight(models, fixtures, config=config)
    except (OSError, ValueError) as exc:
        return _preflight_error(str(exc))

    if args.dry_run:
        print(format_preflight(details))
        return 0

    unpriced_models = [item["model"] for item in details["models"] if item["unpriced"]]
    if unpriced_models and not args.allow_unpriced:
        return _preflight_error("unpriced model(s) require --allow-unpriced: " + ", ".join(unpriced_models))
    if not os.getenv("OPENAI_API_KEY"):
        return _preflight_error("OPENAI_API_KEY must be set in the environment")

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    out_path = args.output_dir / f"eval-openai-script-model-{run_id}.jsonl"
    # Create (and thereby prove writable) the output dir BEFORE the paid run, so a
    # bad --output-dir fails fast instead of after billing N calls and then losing
    # every receipt to a post-payment mkdir/open error.
    try:
        _mkdir_private(args.output_dir)
    except OSError as exc:
        return _preflight_error(f"cannot create output dir {args.output_dir}: {exc}")

    try:
        receipt_fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return _preflight_error(f"cannot create receipt file {out_path}: {exc}")

    with os.fdopen(receipt_fd, "w", encoding="utf-8") as output_file:
        records = asyncio.run(
            run_all(models, fixtures, config=config, api_key=os.environ["OPENAI_API_KEY"], run_id=run_id)
        )
        for record in records:
            output_file.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(summarize(records))
    print(f"\nWrote {len(records)} records to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

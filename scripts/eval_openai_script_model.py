#!/usr/bin/env python3
"""Offline eval harness: compare OpenAI script-generation models for fallback copy.

Forces the OpenAI branch in scriptwriter._generate_json_response by leaving
config.anthropic_api_key empty, then runs a fixed prompt corpus through each
model in MODELS. Captures latency, token usage, JSON validity, and output text
per call. Writes a JSONL log to tmp/evals/ and prints a summary table.

Usage:
    OPENAI_API_KEY=sk-... python scripts/eval_openai_script_model.py
    OPENAI_API_KEY=sk-... python scripts/eval_openai_script_model.py --models gpt-4o-mini gpt-5-mini
    OPENAI_API_KEY=sk-... python scripts/eval_openai_script_model.py --fixtures path/to/prompts.json

Decision criteria stay with the operator — this script ships the means to
evaluate, not the conclusion.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mammamiradio.core.config import load_config  # noqa: E402
from mammamiradio.core.models import StationState, Track  # noqa: E402
from mammamiradio.hosts.scriptwriter import _generate_json_response  # noqa: E402

# Public per-1M-token rates (USD) as of 2026-05. Used for rough cost estimation
# only — verify against your billing dashboard before drawing conclusions.
COST_PER_1M_TOKENS = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
}

DEFAULT_MODELS = ["gpt-4o-mini", "gpt-5-mini"]
DEFAULT_FIXTURES = REPO_ROOT / "scripts" / "eval_fixtures" / "openai_script_prompts.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "tmp" / "evals"


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = COST_PER_1M_TOKENS.get(model)
    if not rates:
        return 0.0
    return prompt_tokens * rates["input"] + completion_tokens * rates["output"] / 1_000_000


async def run_one(*, model: str, fixture: dict, config, state) -> dict:
    config.audio.openai_script_model = model
    record = {
        "model": model,
        "fixture_id": fixture["id"],
        "caller": fixture["caller"],
        "max_tokens": fixture["max_tokens"],
        "json_ok": False,
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
        text = json.dumps(result, ensure_ascii=False)
        record["output_text"] = text
        record["output_chars"] = len(text)
    except Exception as exc:
        record["latency_ms"] = int((time.perf_counter() - t_start) * 1000)
        record["error"] = f"{type(exc).__name__}: {exc}"

    # Token counts come from state deltas. Snapshot before/after each call.
    record["prompt_tokens"] = state._eval_last_prompt_tokens
    record["completion_tokens"] = state._eval_last_completion_tokens
    record["cost_usd"] = estimate_cost_usd(model, record["prompt_tokens"], record["completion_tokens"])
    return record


async def run_all(models: list[str], fixtures: list[dict]) -> list[dict]:
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY must be set in the environment.", file=sys.stderr)
        sys.exit(2)

    config = load_config()
    config.anthropic_api_key = ""  # force OpenAI branch
    config.openai_api_key = os.environ["OPENAI_API_KEY"]

    records: list[dict] = []
    for model in models:
        print(f"\n=== Running model: {model} ===", file=sys.stderr)
        for fixture in fixtures:
            state = StationState(playlist=[Track(title="Eval", artist="Eval", duration_ms=1000, spotify_id="eval")])
            # Snapshot token counters via wrapper attrs so run_one can read deltas.
            before_in = state.api_input_tokens
            before_out = state.api_output_tokens
            wrapped = _TokenSnapshot(state, before_in, before_out)
            record = await run_one(model=model, fixture=fixture, config=config, state=wrapped)
            print(
                f"  {fixture['id']:32s} caller={fixture['caller']:11s} "
                f"json_ok={record['json_ok']!s:5s} latency={record['latency_ms']}ms "
                f"tokens={record['prompt_tokens']}/{record['completion_tokens']}"
                + (f" error={record['error']}" if record["error"] else ""),
                file=sys.stderr,
            )
            records.append(record)
    return records


class _TokenSnapshot:
    """Lightweight wrapper that exposes per-call token deltas after the LLM call.

    StationState mutates api_input_tokens / api_output_tokens cumulatively. We
    only want the delta for this specific call, so we capture the pre-call
    counters and expose the diff via _eval_last_* attrs.
    """

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


def summarize(records: list[dict]) -> str:
    by_model: dict[str, list[dict]] = {}
    for r in records:
        by_model.setdefault(r["model"], []).append(r)

    lines = []
    lines.append("")
    lines.append("=" * 100)
    lines.append("SUMMARY")
    lines.append("=" * 100)
    header = (
        f"{'model':16s} {'n':>3s} {'json_ok':>8s} {'p50_ms':>8s} {'p95_ms':>8s} "
        f"{'mean_chars':>10s} {'in_tok':>7s} {'out_tok':>8s} {'cost_usd':>10s}"
    )
    lines.append(header)
    lines.append("-" * 100)
    for model, recs in by_model.items():
        n = len(recs)
        ok = sum(1 for r in recs if r["json_ok"])
        latencies = sorted(r["latency_ms"] for r in recs if r["latency_ms"] is not None)
        p50 = statistics.median(latencies) if latencies else 0
        p95 = latencies[min(len(latencies) - 1, math.ceil(0.95 * (len(latencies) - 1)))] if latencies else 0
        char_records = [r["output_chars"] for r in recs if r["output_chars"]]
        mean_chars = statistics.mean(char_records) if char_records else 0
        in_tok = sum(r["prompt_tokens"] for r in recs)
        out_tok = sum(r["completion_tokens"] for r in recs)
        cost = sum(r["cost_usd"] for r in recs)
        ok_cell = f"{ok}/{n}"
        lines.append(
            f"{model:16s} {n:3d} {ok_cell:>8s} {int(p50):8d} {int(p95):8d} "
            f"{int(mean_chars):10d} {in_tok:7d} {out_tok:8d} ${cost:9.4f}"
        )
    lines.append("=" * 100)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS, help="OpenAI model IDs to evaluate")
    ap.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, help="Prompt corpus JSON path")
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Where to write the JSONL log")
    args = ap.parse_args(argv)

    fixtures = json.loads(args.fixtures.read_text())
    if not isinstance(fixtures, list) or not fixtures:
        print(f"ERROR: {args.fixtures} must contain a non-empty list of fixtures.", file=sys.stderr)
        return 2
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY must be set in the environment.", file=sys.stderr)
        return 2

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.output_dir / f"eval-openai-script-model-{timestamp}.jsonl"

    records = asyncio.run(run_all(args.models, fixtures))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(summarize(records))
    print(f"\nWrote {len(records)} records to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

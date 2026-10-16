#!/usr/bin/env python3
"""Benchmark the Haiku fast path: the incumbent Haiku 4.5 against Haiku 5.5.

The model registry pins the ``fast`` role (transitions, post-air memory
extraction) to Claude Haiku 4.5, and the registry loader drops any effort entry
whose catalog model starts with ``claude-haiku`` (``EFFORT_UNSUPPORTED_MODEL_PREFIX``
in ``core/config.py``), because Haiku 4.5 rejects ``output_config.effort`` with a
400. The production path can therefore never try Haiku 5.5 at a chosen effort.
This command calls the Anthropic SDK directly, bypassing ``resolve_model()`` /
``effort_for()`` and the scriptwriter's provider plumbing on purpose, and runs
four arms over the same production-built prompts:

    A  incumbent: the registry's fast-role model, no output_config (what production sends)
    B  claude-haiku-5-5, effort low
    C  claude-haiku-5-5, effort medium
    D  claude-haiku-5-5, default (no output_config; documented default effort is medium)

Arms C and D are nominally the same server-side configuration, so their spread
doubles as a repeatability estimate.

It is an online, paid operator command. It is not a CI gate, not runtime
enforcement, and not a bump of ``model_registry.toml``. Always run the offline
preview first:

    python scripts/bench_haiku_fast_path.py --dry-run
    MAMMAMIRADIO_BENCH_ANTHROPIC_API_KEY=... python scripts/bench_haiku_fast_path.py

The live run reads ``MAMMAMIRADIO_BENCH_ANTHROPIC_API_KEY`` first and falls back
to ``ANTHROPIC_API_KEY``, from the shell environment only (never ``.env``). The
dry run reads neither, and the production config loader never sees a provider
key while the corpus is built.

The gate is latency only (p95 within +10 % of the incumbent per prompt type,
and no more unusable or too-long outputs than the incumbent). Cost is reported,
never gated. Receipts land under ``tmp/evals/`` (gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import math
import os
import re
import statistics
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "tmp" / "evals"


def _display_path(path: Path) -> str:
    """Render a path relative to the repository root so receipts and reports carry no machine-specific prefix."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


RADIO_TOML = REPO_ROOT / "radio.toml"
STARTER_CATALOG = REPO_ROOT / "mammamiradio" / "assets" / "starter" / "catalog.json"
SPOKEN_ASSETS = REPO_ROOT / "mammamiradio" / "assets" / "demo" / "spoken_assets.json"

RECEIPT_SCHEMA_VERSION = 1
PROMPTS_PER_TYPE = 50
TRANSITION_MAX_TOKENS = 100  # scriptwriter.write_transition sends max_tokens=100
TRANSITION_MAX_WORDS = 15  # the transition prompt's own "Max 15 words" rule
TRANSITION_SUPER_ITALIAN_COUNT = 15
MEMORY_PERSONA_NONE_COUNT = 20
MEMORY_PINNED_COUNT = 25
RETRY_STATUSES = frozenset({429, 529})
MAX_RETRIES_PER_CALL = 2
DEFAULT_RETRY_SLEEP_S = 5.0
LATENCY_HEADROOM = 1.10
P95_METHOD = "nearest-rank (ceil(0.95*n)-th sorted value)"

# A bench-specific name lets the benchmark run on its own key rather than the
# station's; the live run reads it first and falls back to the standard name.
# Both are read from the shell environment only, never from .env.
BENCH_KEY_ENV = "MAMMAMIRADIO_BENCH_ANTHROPIC_API_KEY"
FALLBACK_KEY_ENV = "ANTHROPIC_API_KEY"
# Every provider key the production config loader would otherwise read while
# the corpus is built. Hidden from it; the corpus never makes a provider call.
PROVIDER_KEY_ENVS = (BENCH_KEY_ENV, FALLBACK_KEY_ENV, "OPENAI_API_KEY")

# Candidate successor confirmed from the published model overview
# (https://platform.claude.com/docs/en/models/overview.md, captured 2026-10-16):
# the Claude API ID row lists `claude-haiku-5-5`, dateless and pinned, and the
# "Default effort" row lists `medium` for it, so arm D (no output_config) is
# nominally the same server-side configuration as arm C (effort medium).
CANDIDATE_MODEL = "claude-haiku-5-5"
CANDIDATE_DEFAULT_EFFORT = "medium"
MODEL_DOCS_URL = "https://platform.claude.com/docs/en/models/overview.md"
MODEL_DOCS_CAPTURED = "2026-10-16"

# Haiku 5.5 list price for prompts up to 100,000 tokens, from the published
# pricing page (https://platform.claude.com/docs/en/about-claude/pricing.md,
# captured 2026-10-16). Every prompt here is far below that threshold.
PRICING_DOCS_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
PRICING_DOCS_CAPTURED = "2026-10-16"
HAIKU_55_INPUT_PER_MILLION = 0.10
HAIKU_55_OUTPUT_PER_MILLION = 0.50
# The row is accepted only under this heading and only in the six-column
# standard shape (model, base input, 5m cache writes, 1h cache writes, cache
# hits, output). The batch table further down repeats the same row label at
# half price in a two-column shape; matching it would silently halve the cost.
_PRICING_TABLE_HEADING = "## Model pricing"
_PRICE_CELL = r"\$[0-9.]+ / MTok(?:<sup>\d+</sup>)?"
_HAIKU_55_STANDARD_ROW = re.compile(
    r"^\|\s*Claude Haiku 5\.5 \(for prompts up to 100,000 tokens\)\s*"
    r"\|\s*\$(?P<input>[0-9.]+) / MTok(?:<sup>\d+</sup>)?\s*"  # base input tokens
    + rf"\|\s*{_PRICE_CELL}\s*" * 3  # 5m cache writes, 1h cache writes, cache hits and refreshes
    + r"\|\s*\$(?P<output>[0-9.]+) / MTok(?:<sup>\d+</sup>)?\s*\|\s*$"  # output tokens
)

TRANSITION_CODE_PATH = (
    "mammamiradio.hosts.scriptwriter.write_transition -> _generate_json_response_with_language_guard (stubbed)"
)
TRANSITION_SEGMENTS = ("banter", "ad", "news_flash")
TRANSITION_STYLES = ("react", "echo", "exclaim")

# Prompt material in the voice of the reviewed demo banter transcripts
# (Studio B, the espresso machine, the corridor, Nico).
_SONG_CUE_BANK: tuple[dict[str, str], ...] = (
    {"type": "reaction", "text": "Giulia's mug came back to the Studio B shelf during this one"},
    {"type": "reaction", "text": "Marco once danced with the broken-wheel Studio B chair to this track"},
    {"type": "mood", "text": "the espresso machine goes quiet on this intro, which Marco calls a review"},
    {"type": "note", "text": "the rooftop antenna above Windor seems to lean into the chorus"},
)
_PERSONA_BLOBS: tuple[str, ...] = (
    "Theory: the espresso machine curates the playlist and demands on-air credit. Callback: 'check your cup'.",
    "Theory: Studio B's third chair is for whoever is listening. Joke: Nico's postcard travelled four metres.",
    "Personality guess: Marco wants a tribunal for every Studio B manual rule; Giulia never argues with the red lamp.",
    "Callback used: Signora Cattaneo's espresso scorecard (Marco 5.2, with the comma). Theory: the mic is jealous.",
    "Joke: the Archive receipt with seven emergency lemons and a 'dramatic use only' bell. Marco's handwriting.",
    "Theory: the rooftop antenna broadcasts into deep space for better listeners. Callback: 'the antenna knows'.",
)
_INTERACTION_CONTEXTS: tuple[dict[str, Any], ...] = (
    {"listener_request": "none", "reactive_directive": ""},
    {"segment_type": "banter", "next_segment": "ad"},
    {"time_of_day": "late evening", "weekend": False},
    {"listener_request": "shout-out for the night shift", "callback_gag": ""},
)
# Real song identities from the starter catalog plus the repo's own test
# fixtures; nothing beyond those lists is invented here.
_FIXTURE_TRACKS: tuple[tuple[str, str], ...] = (
    ("Volare", "Domenico Modugno"),
    ("L'Estate", "Vivaldi"),
    ("Albachiara", "Vasco Rossi"),
    ("Azzurro", "Adriano Celentano"),
    ("Con te partirò!", "Andrea Bocelli"),
    ("Felicità", "Al Bano"),
    ("Più bella cosa", "Eros Ramazzotti"),
    ("Sapore di Sale", "Gino Paoli"),
    ("Bye Bye Bye", "NSYNC"),
    ("Fighter", "Christina Aguilera"),
    ("Toxic", "Britney Spears"),
    ("Miss Understanding", "Sam Brown"),
    ("Dont Lose Your Way", "Fleece"),
    ("Audio", "LSD"),
    ("Ordinary", "Alex Warren"),
    ("Thunder", "AC/DC"),
    ("Estate", "Bruno"),
    ("Canzone Uno", "Artista"),
    ("Canzone Due", "Artista"),
    ("Vecchia Canzone", "Artista Antico"),
    ("Appena Suonata", "Artista"),
    ("Nuova", "Artista"),
    ("Prima", "Artista"),
    ("Seconda", "Artista"),
    ("Timing Song", "Artista"),
    ("Brano", "Artista"),
    ("Song Alpha", "Art1"),
    ("Song Beta", "Art2"),
    ("Epic", "Band"),
    ("Long Mix", "DJ"),
    ("PopSong", "PopArtist"),
    ("Priority Song", "VIP"),
    ("Seed Song", "Seed Artist"),
    ("Pinned Song", "Pinned Artist"),
    ("Chart Song", "Chart Artist"),
    ("Fresh Song", "Fresh Artist"),
    ("Local Song", "Artist"),
    ("Test Song", "Test Artist"),
)


@dataclass(frozen=True)
class Arm:
    label: str
    model: str
    effort: str | None
    description: str


@dataclass
class BenchPrompt:
    prompt_id: str
    prompt_type: str
    caller: str
    prompt: str
    system: str
    max_tokens: int
    super_italian: bool
    code_path: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()

    @property
    def system_sha256(self) -> str:
        return hashlib.sha256(self.system.encode("utf-8")).hexdigest()


@dataclass
class Pricing:
    """Per-token prices for one model, or ``None`` when deliberately unpriced."""

    input_per_million: float | None
    output_per_million: float | None
    source: str
    captured: str

    @property
    def priced(self) -> bool:
        return self.input_per_million is not None and self.output_per_million is not None

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float | None:
        if self.input_per_million is None or self.output_per_million is None:
            return None
        return (input_tokens * self.input_per_million + output_tokens * self.output_per_million) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CallResult:
    latency_s: float
    attempts: int
    retried: bool
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    output_text: str
    error: str | None
    timed_out: bool = False


def _error_label(exc: BaseException) -> str:
    """Name a failed call for the receipt: class, HTTP status when present, and the start of the message."""
    status = getattr(exc, "status_code", None)
    label = type(exc).__name__ if status is None else f"{type(exc).__name__} {status}"
    head = " ".join(str(exc).split())[:160]
    return f"{label}: {head}" if head else label


class CorpusError(RuntimeError):
    """The benchmark corpus failed validation before any provider call."""


class _PromptCapturedError(Exception):
    """Sentinel raised by the provider stub once the production prompt is recorded."""


def live_api_key() -> str:
    """The live-run key from the shell environment only; empty when neither name is set.

    Called before anything under ``mammamiradio`` is imported, so the
    production loader's import-time ``load_dotenv()`` cannot have copied a
    ``.env`` value into ``os.environ`` yet. The value is never printed or written.
    """
    for name in (BENCH_KEY_ENV, FALLBACK_KEY_ENV):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


@contextlib.contextmanager
def _without_provider_keys() -> Iterator[None]:
    """Hide every provider key from ``load_config()`` while the corpus is built.

    ``mammamiradio.core.config`` runs ``load_dotenv()`` once at import and
    ``load_config()`` reads the keys from ``os.environ`` at call time, so the
    names are removed after that import and before the call. The shell's own
    values come back afterwards; anything ``.env`` injected does not, because
    the live run reads the shell environment only.
    """
    saved = {name: os.environ.get(name) for name in PROVIDER_KEY_ENVS}
    try:
        import mammamiradio.core.config  # noqa: F401  # runs load_dotenv() once

        for name in PROVIDER_KEY_ENVS:
            os.environ.pop(name, None)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ---------------------------------------------------------------------------
# Corpus: production-built prompts, no network
# ---------------------------------------------------------------------------


def _starter_tracks() -> list[tuple[str, str]]:
    data = json.loads(STARTER_CATALOG.read_text(encoding="utf-8"))
    tracks = data.get("tracks") if isinstance(data, dict) else None
    if not isinstance(tracks, list):
        raise CorpusError(f"starter catalog has no tracks list: {STARTER_CATALOG}")
    pairs: list[tuple[str, str]] = []
    for row in tracks:
        if isinstance(row, dict) and isinstance(row.get("title"), str) and isinstance(row.get("artist"), str):
            pairs.append((row["title"], row["artist"]))
    if not pairs:
        raise CorpusError("starter catalog yielded no title/artist pairs")
    return pairs


def _distinct_tracks(count: int) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for pair in [*_starter_tracks(), *_FIXTURE_TRACKS]:
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    if len(pairs) < count:
        raise CorpusError(f"only {len(pairs)} distinct tracks available, need {count}")
    return pairs[:count]


@dataclass
class ModeContext:
    """Both station modes plus the production system prompt built once per mode.

    ``_build_system_prompt`` picks one example host at random, so rebuilding it
    for every captured prompt would hand each arm a slightly different prefix.
    Production builds it once per config change; the corpus does the same.
    """

    normal: Any
    italian: Any
    system: dict[bool, str]

    def config(self, super_italian: bool) -> Any:
        return self.italian if super_italian else self.normal


def load_mode_context() -> ModeContext:
    """Load radio.toml twice: Normal Mode and Super Italian Mode.

    The stand-in key only satisfies ``has_script_llm``; the corpus builder
    never makes a provider call and the live run takes its key from the
    environment, never from config. The loader runs with every provider key
    hidden, so neither the shell's key nor a ``.env`` key is read here.
    """
    with _without_provider_keys():
        from mammamiradio.core.config import load_config
        from mammamiradio.hosts import scriptwriter

        # Config loading warns about the deliberately absent provider keys; the
        # stubbed writer logs each swallowed sentinel. Neither is a benchmark event.
        logging.getLogger("mammamiradio").setLevel(logging.CRITICAL)
        normal = load_config(str(RADIO_TOML))
        italian = load_config(str(RADIO_TOML))
    normal.anthropic_api_key = "bench-stand-in-not-a-key"
    normal.openai_api_key = ""
    normal.super_italian_mode = False
    italian.anthropic_api_key = "bench-stand-in-not-a-key"
    italian.openai_api_key = ""
    italian.super_italian_mode = True
    system = {
        False: scriptwriter._get_system_prompt(normal),
        True: scriptwriter._get_system_prompt(italian),
    }
    return ModeContext(normal=normal, italian=italian, system=system)


def build_transition_corpus(modes: ModeContext, count: int = PROMPTS_PER_TYPE) -> list[BenchPrompt]:
    """Capture ``write_transition`` prompts through the production writer.

    The provider function the writer awaits is replaced by a stub that records
    prompt, system prompt, model, max_tokens and caller, then raises a sentinel;
    the writer swallows it and returns stock copy, so nothing leaves the process.
    """
    from mammamiradio.core.models import StationState, Track
    from mammamiradio.hosts import scriptwriter
    from mammamiradio.hosts.transitions import _transition_stock_fallbacks

    tracks = _distinct_tracks(count)
    regular_hosts = scriptwriter._regular_hosts(modes.normal)
    if len(regular_hosts) < 2:
        raise CorpusError("radio.toml needs at least two regular hosts for the transition corpus")

    captured: list[dict[str, Any]] = []

    async def _stub(**kwargs: Any) -> dict[str, Any]:
        config = kwargs["config"]
        captured.append(
            {
                "prompt": kwargs["prompt"],
                "system": modes.system[bool(config.super_italian_mode)],
                "model": kwargs["model"],
                "max_tokens": kwargs["max_tokens"],
                "caller": kwargs["caller"],
            }
        )
        raise _PromptCapturedError()

    original_provider = scriptwriter._generate_json_response_with_language_guard
    original_regular_hosts = scriptwriter._regular_hosts
    prompts: list[BenchPrompt] = []
    try:
        scriptwriter._generate_json_response_with_language_guard = _stub  # type: ignore[assignment]
        for index, (title, artist) in enumerate(tracks):
            super_italian = index % 10 in (3, 6, 9)
            config = modes.config(super_italian)
            host = regular_hosts[index % 2]
            next_segment = TRANSITION_SEGMENTS[index % 3]
            style = TRANSITION_STYLES[(index // 3) % 3]
            with_recent = index % 4 in (1, 2)
            with_cues = style == "exclaim" or index % 4 == 0
            track = Track(title=title, artist=artist, duration_ms=180_000, source="starter")
            state = StationState(playlist=[track])
            state.played_tracks.append(track)
            if with_recent:
                stock = _transition_stock_fallbacks(super_italian=super_italian)
                recent = [stock[key] for key in sorted(stock)][: 2 + index % 2]
                state.recent_transition_texts.extend(recent)
            cues = [_SONG_CUE_BANK[(index + offset) % len(_SONG_CUE_BANK)] for offset in range(1 + index % 2)]
            song_cues: list[dict] = [dict(cue) for cue in cues] if with_cues else []

            def _pinned_host(_config: Any, _host: Any = host) -> list[Any]:
                return [_host]

            scriptwriter._regular_hosts = _pinned_host  # type: ignore[assignment]
            before = len(captured)
            asyncio.run(
                scriptwriter.write_transition(
                    state,
                    config,
                    next_segment=next_segment,
                    style=style,
                    song_cues=song_cues,
                )
            )
            if len(captured) != before + 1:
                raise CorpusError(f"transition writer did not reach the provider for prompt {index}")
            record = captured[-1]
            if record["caller"] != "transition" or record["max_tokens"] != TRANSITION_MAX_TOKENS:
                raise CorpusError(
                    f"transition writer sent caller={record['caller']!r} max_tokens={record['max_tokens']!r}"
                )
            prompts.append(
                BenchPrompt(
                    prompt_id=f"transition-{index:02d}",
                    prompt_type="transition",
                    caller="transition",
                    prompt=record["prompt"],
                    system=record["system"],
                    max_tokens=record["max_tokens"],
                    super_italian=super_italian,
                    code_path=TRANSITION_CODE_PATH,
                    meta={
                        "track": f"{artist} – {title}",
                        "host": host.name,
                        "next_segment": next_segment,
                        "style": style,
                        "recent_texts": with_recent,
                        "song_cues": len(song_cues),
                        "registry_model": record["model"],
                    },
                )
            )
    finally:
        scriptwriter._generate_json_response_with_language_guard = original_provider  # type: ignore[assignment]
        scriptwriter._regular_hosts = original_regular_hosts  # type: ignore[assignment]
    return prompts


@dataclass
class _Transcript:
    path: str
    language: str
    lines: list[dict[str, str]]


def _demo_banter_transcripts(host_names: Sequence[str]) -> list[_Transcript]:
    """Split the reviewed demo banter transcripts into speaker lines.

    The speaker alternation comes from the regular-host roster in ``radio.toml``
    rather than a hard-coded pair, so a renamed host fails the corpus loudly
    (no transcript reaches four lines) instead of silently shrinking it.
    """
    data = json.loads(SPOKEN_ASSETS.read_text(encoding="utf-8"))
    assets = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(assets, list):
        raise CorpusError(f"spoken assets manifest has no assets list: {SPOKEN_ASSETS}")
    names = [name for name in host_names if name.strip()]
    if not names:
        raise CorpusError("radio.toml yielded no regular host names for the transcript speaker alternation")
    speakers = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    line_re = re.compile(rf"({speakers}): (.*?)(?=(?:{speakers}): |$)", re.S)
    transcripts: list[_Transcript] = []
    for asset in assets:
        if not isinstance(asset, dict) or not str(asset.get("path", "")).startswith("banter/"):
            continue
        transcript = str(asset.get("transcript") or "")
        lines = [{"host": m.group(1), "text": m.group(2).strip()} for m in line_re.finditer(transcript)]
        lines = [line for line in lines if line["text"]]
        if len(lines) >= 4:
            transcripts.append(
                _Transcript(path=str(asset["path"]), language=str(asset.get("language", "")), lines=lines)
            )
    if not transcripts:
        raise CorpusError("no reviewed demo banter transcripts with at least four lines")
    return transcripts


def _banter_windows(transcripts: Sequence[_Transcript], count: int) -> list[tuple[_Transcript, int, int]]:
    """Pick ``count`` distinct 4-10 line windows, round-robin across the transcripts.

    Each transcript's windows are visited in a deterministic pseudo-shuffled
    order so the corpus mixes window lengths and offsets instead of always
    taking the opening lines.
    """
    per_transcript: list[list[tuple[int, int]]] = []
    for index, transcript in enumerate(transcripts):
        total = len(transcript.lines)
        windows = [(start, length) for length in range(4, min(10, total) + 1) for start in range(0, total - length + 1)]

        def _shuffle_key(window: tuple[int, int], salt: int = index) -> tuple[int, int, int]:
            start, length = window
            return ((start * 7 + length * 3 + salt) % 11, start, length)

        windows.sort(key=_shuffle_key)
        per_transcript.append(windows)
    picked: list[tuple[_Transcript, int, int]] = []
    cursor = [0] * len(transcripts)
    attempt = 0
    while len(picked) < count:
        if attempt > count * len(transcripts):
            raise CorpusError("could not derive enough distinct banter windows from the demo transcripts")
        slot = attempt % len(transcripts)
        attempt += 1
        windows = per_transcript[slot]
        if cursor[slot] >= len(windows):
            continue
        start, length = windows[cursor[slot]]
        cursor[slot] += 1
        picked.append((transcripts[slot], start, length))
    return picked


def build_memory_corpus(modes: ModeContext, count: int = PROMPTS_PER_TYPE) -> list[BenchPrompt]:
    """Build ``MemoryExtractionCommit`` payloads and run the production prompt builder.

    Each commit is round-tripped through ``to_metadata()`` / ``from_metadata()``,
    the pair the producer writes into segment metadata and the extractor reads
    back, so the prompt bytes equal what production sends.
    """
    from mammamiradio.hosts import scriptwriter
    from mammamiradio.hosts.memory_extractor import (
        MEMORY_EXTRACT_CALLER,
        MEMORY_EXTRACT_MAX_TOKENS,
        MemoryExtractionCommit,
        _build_prompt,
    )

    transcripts = _demo_banter_transcripts([host.name for host in scriptwriter._regular_hosts(modes.normal)])
    pin_slugs = [
        re.sub(r"[^a-z0-9]+", "-", f"{artist} {title}".lower()).strip("-") for title, artist in _starter_tracks()
    ]
    prompts: list[BenchPrompt] = []
    seen_scripts: set[str] = set()
    for transcript, start, length in _banter_windows(transcripts, count):
        window = transcript.lines[start : start + length]
        script_key = json.dumps(window, ensure_ascii=False)
        if script_key in seen_scripts:
            raise CorpusError(f"duplicate banter window {transcript.path}[{start}:{start + length}]")
        seen_scripts.add(script_key)

        index = len(prompts)
        persona_none = index % 5 in (0, 2)
        pinned = index % 2 == 0
        authored = MemoryExtractionCommit(
            script_lines=[dict(line) for line in window],
            persona_context="" if persona_none else _PERSONA_BLOBS[index % len(_PERSONA_BLOBS)],
            interaction_context=dict(_INTERACTION_CONTEXTS[index % len(_INTERACTION_CONTEXTS)]),
            youtube_id=f"bench-pin-{pin_slugs[index % len(pin_slugs)]}" if pinned else "",
            source_session=index + 1,
        )
        commit = MemoryExtractionCommit.from_metadata(authored.to_metadata())
        if commit is None:
            raise CorpusError(f"memory commit {index} did not survive the production metadata round-trip")
        super_italian = transcript.language == "it"
        prompts.append(
            BenchPrompt(
                prompt_id=f"memory-{index:02d}",
                prompt_type="memory_extract",
                caller=MEMORY_EXTRACT_CALLER,
                prompt=_build_prompt(commit),
                system=modes.system[super_italian],
                max_tokens=MEMORY_EXTRACT_MAX_TOKENS,
                super_italian=super_italian,
                code_path=(
                    "mammamiradio.hosts.memory_extractor._build_prompt("
                    "MemoryExtractionCommit.from_metadata(commit.to_metadata()))"
                ),
                meta={
                    "transcript": transcript.path,
                    "window": [start, start + length],
                    "lines": length,
                    "persona_context": not persona_none,
                    "pinned": pinned,
                    "interaction_context": sorted(commit.interaction_context),
                },
            )
        )
    return prompts


def validate_corpus(prompts: Sequence[BenchPrompt]) -> None:
    by_type: dict[str, list[BenchPrompt]] = {}
    for prompt in prompts:
        by_type.setdefault(prompt.prompt_type, []).append(prompt)
    for prompt_type in ("transition", "memory_extract"):
        rows = by_type.get(prompt_type, [])
        if len(rows) != PROMPTS_PER_TYPE:
            raise CorpusError(f"{prompt_type}: expected {PROMPTS_PER_TYPE} prompts, built {len(rows)}")
        if len({row.prompt_id for row in rows}) != len(rows):
            raise CorpusError(f"{prompt_type}: duplicate prompt ids")
        if len({row.prompt_sha256 for row in rows}) != len(rows):
            raise CorpusError(f"{prompt_type}: duplicate prompt texts")
        if len({row.system_sha256 for row in rows}) > 2:
            raise CorpusError(f"{prompt_type}: more than one system prompt per station mode")
        if any(not row.prompt.strip() or not row.system.strip() for row in rows):
            raise CorpusError(f"{prompt_type}: empty prompt or system prompt")
    transitions = by_type["transition"]
    italian = sum(1 for row in transitions if row.super_italian)
    if italian != TRANSITION_SUPER_ITALIAN_COUNT:
        raise CorpusError(
            f"transition: expected {TRANSITION_SUPER_ITALIAN_COUNT} Super Italian prompts, built {italian}"
        )
    if len({row.meta["host"] for row in transitions}) < 2:
        raise CorpusError("transition: both hosts must appear")
    if sum(1 for row in transitions if row.meta["recent_texts"]) != PROMPTS_PER_TYPE // 2:
        raise CorpusError("transition: half the prompts must carry recent transition texts")
    memory = by_type["memory_extract"]
    if sum(1 for row in memory if not row.meta["persona_context"]) != MEMORY_PERSONA_NONE_COUNT:
        raise CorpusError(f"memory_extract: expected {MEMORY_PERSONA_NONE_COUNT} commits without persona context")
    if sum(1 for row in memory if row.meta["pinned"]) != MEMORY_PINNED_COUNT:
        raise CorpusError(f"memory_extract: expected {MEMORY_PINNED_COUNT} pinned commits")
    if any(not 4 <= row.meta["lines"] <= 10 for row in memory):
        raise CorpusError("memory_extract: every script window must have 4 to 10 lines")


# ---------------------------------------------------------------------------
# Arms, pricing, timeout, effort mechanism
# ---------------------------------------------------------------------------


def incumbent_model() -> str:
    """The fast-role model production resolves today, straight from the registry."""
    with _without_provider_keys():
        from mammamiradio.core.config import load_config, resolve_model

        config = load_config(str(RADIO_TOML))
    model = resolve_model(config.models, "transition", "anthropic")
    if not model:
        raise CorpusError("model_registry.toml does not resolve an Anthropic transition model")
    memory_model = resolve_model(config.models, "memory_extract", "anthropic")
    if memory_model != model:
        raise CorpusError(f"transition ({model}) and memory_extract ({memory_model}) resolve to different fast models")
    return model


def build_arms(incumbent: str, candidate: str = CANDIDATE_MODEL) -> list[Arm]:
    return [
        Arm("A", incumbent, None, "incumbent, no output_config (what production sends today)"),
        Arm("B", candidate, "low", "candidate, effort low"),
        Arm("C", candidate, "medium", "candidate, effort medium"),
        Arm(
            "D",
            candidate,
            None,
            f"candidate, default (no output_config; documented default effort {CANDIDATE_DEFAULT_EFFORT})",
        ),
    ]


def detect_effort_mechanism() -> str:
    """``output_config`` when the installed SDK exposes it on messages.create, else ``extra_body``.

    Inspecting the signature constructs no client and performs no network IO.
    """
    from anthropic.resources.messages import AsyncMessages

    params = inspect.signature(AsyncMessages.create).parameters
    return "output_config" if "output_config" in params else "extra_body"


def attempt_timeout(max_tokens: int) -> float:
    """Production's per-attempt wall clock for the requested budget."""
    from mammamiradio.hosts.scriptwriter import _attempt_timeout

    return float(_attempt_timeout(max_tokens))


def parse_haiku55_pricing(markdown: str) -> tuple[float, float] | None:
    """Haiku 5.5 list price from the standard model-pricing table, or ``None``.

    Only a row under the ``## Model pricing`` heading in the six-column standard
    shape counts; any other heading ends the table. The batch table repeats the
    same row label at half price, so a reordered or restructured page leaves the
    candidate unpriced instead of silently halving its cost.
    """
    in_standard_table = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            in_standard_table = line == _PRICING_TABLE_HEADING
            continue
        if not in_standard_table:
            continue
        match = _HAIKU_55_STANDARD_ROW.match(line)
        if match:
            return float(match.group("input")), float(match.group("output"))
    return None


def load_pricing(incumbent: str, capture: Path | None, capture_date: str | None) -> dict[str, Pricing]:
    """Registry price for the incumbent; documented list price for the candidate."""
    with _without_provider_keys():
        from mammamiradio.core.config import load_config

        config = load_config(str(RADIO_TOML))
    input_rate, output_rate, unpriced = config.models.price_for_model(incumbent)
    incumbent_pricing = Pricing(
        input_per_million=None if unpriced else round(input_rate * 1_000_000, 6),
        output_per_million=None if unpriced else round(output_rate * 1_000_000, 6),
        source="model_registry.toml [pricing.catalog.anthropic]",
        captured="registry",
    )
    candidate_pricing = Pricing(
        input_per_million=HAIKU_55_INPUT_PER_MILLION,
        output_per_million=HAIKU_55_OUTPUT_PER_MILLION,
        source=PRICING_DOCS_URL,
        captured=PRICING_DOCS_CAPTURED,
    )
    if capture is not None:
        label = capture_date or datetime.now(UTC).date().isoformat()
        try:
            parsed = parse_haiku55_pricing(capture.read_text(encoding="utf-8"))
        except OSError:
            parsed = None
        if parsed is None:
            candidate_pricing = Pricing(
                None, None, f"{_display_path(capture)} (Haiku 5.5 row not found; unpriced)", label
            )
        else:
            candidate_pricing = Pricing(parsed[0], parsed[1], _display_path(capture), label)
    return {incumbent: incumbent_pricing, CANDIDATE_MODEL: candidate_pricing}


# ---------------------------------------------------------------------------
# Quality flags, computed with the production validators
# ---------------------------------------------------------------------------


def _parse_json_output(raw: str) -> Any:
    from mammamiradio.hosts.scriptwriter import _strip_fences

    return json.loads(_strip_fences(raw.strip()))


def evaluate_transition(raw: str, stop_reason: str | None) -> dict[str, Any]:
    from mammamiradio.hosts.transitions import _transition_text_usable

    too_long = stop_reason == "max_tokens"
    try:
        data = _parse_json_output(raw)
    except (json.JSONDecodeError, IndexError):
        return {"usable": False, "too_long": too_long, "parsed": False, "word_count": None}
    text = data.get("text") if isinstance(data, dict) else None
    usable = isinstance(text, str) and bool(_transition_text_usable(text))
    word_count = len(text.split()) if isinstance(text, str) else None
    if word_count is not None and word_count > TRANSITION_MAX_WORDS:
        too_long = True
    return {"usable": usable, "too_long": too_long, "parsed": True, "word_count": word_count}


def evaluate_memory(raw: str, stop_reason: str | None) -> dict[str, Any]:
    from mammamiradio.hosts.memory_extractor import _has_persona_updates, _song_cues_from_response

    too_long = stop_reason == "max_tokens"
    try:
        data = _parse_json_output(raw)
    except (json.JSONDecodeError, IndexError):
        return {"usable": False, "too_long": too_long, "parsed": False}
    if not isinstance(data, dict):
        return {"usable": False, "too_long": too_long, "parsed": True}
    persona_updates = data.get("persona_updates", {})
    song_cues = data.get("song_cues", [])
    shape_ok = isinstance(persona_updates, dict) and isinstance(song_cues, list)
    if shape_ok:
        for key in ("new_theories", "new_personality_guesses", "new_jokes", "callbacks_used"):
            if key in persona_updates and not isinstance(persona_updates.get(key), list):
                shape_ok = False
    # A well-formed empty result is usable: the prompt asks for empty arrays
    # when nothing durable was established.
    return {
        "usable": shape_ok,
        "too_long": too_long,
        "parsed": True,
        "has_persona_updates": bool(_has_persona_updates(persona_updates)),
        "song_cue_count": len(_song_cues_from_response(data)),
    }


def evaluate_output(prompt_type: str, raw: str, stop_reason: str | None) -> dict[str, Any]:
    if prompt_type == "transition":
        return evaluate_transition(raw, stop_reason)
    return evaluate_memory(raw, stop_reason)


# ---------------------------------------------------------------------------
# Live run: one request in flight at any moment
# ---------------------------------------------------------------------------


def build_create_kwargs(arm: Arm, prompt: BenchPrompt, mechanism: str) -> dict[str, Any]:
    """Mirror ``scriptwriter._generate_json_response``'s request shape."""
    kwargs: dict[str, Any] = {
        "model": arm.model,
        "max_tokens": prompt.max_tokens,
        "system": prompt.system,
        "messages": [{"role": "user", "content": prompt.prompt}],
    }
    if arm.effort is not None:
        if mechanism == "output_config":
            kwargs["output_config"] = {"effort": arm.effort}
        else:
            kwargs["extra_body"] = {"output_config": {"effort": arm.effort}}
    return kwargs


def _retry_after_seconds(exc: Any) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    raw = headers.get("retry-after") if headers is not None else None
    try:
        value = float(raw) if raw is not None else DEFAULT_RETRY_SLEEP_S
    except (TypeError, ValueError):
        value = DEFAULT_RETRY_SLEEP_S
    return max(0.0, min(value, 60.0))


async def call_arm(
    create: Callable[..., Awaitable[Any]],
    arm: Arm,
    prompt: BenchPrompt,
    mechanism: str,
    timeout_s: float,
) -> CallResult:
    import anthropic

    from mammamiradio.hosts.scriptwriter import _anthropic_text

    kwargs = build_create_kwargs(arm, prompt, mechanism)
    attempts = 0
    while True:
        attempts += 1
        started = time.perf_counter()
        try:
            resp = await asyncio.wait_for(create(**kwargs), timeout=timeout_s)
        except anthropic.APIStatusError as exc:
            latency = time.perf_counter() - started
            if exc.status_code in RETRY_STATUSES and attempts <= MAX_RETRIES_PER_CALL:
                await asyncio.sleep(_retry_after_seconds(exc))
                continue
            return CallResult(latency, attempts, attempts > 1, None, 0, 0, "", _error_label(exc))
        except (TimeoutError, anthropic.APITimeoutError) as exc:
            # The production per-attempt wall clock (asyncio.wait_for) or the
            # SDK's own transport timeout: an error row, counted separately.
            latency = time.perf_counter() - started
            return CallResult(latency, attempts, attempts > 1, None, 0, 0, "", _error_label(exc), timed_out=True)
        except Exception as exc:
            latency = time.perf_counter() - started
            return CallResult(latency, attempts, attempts > 1, None, 0, 0, "", _error_label(exc))
        latency = time.perf_counter() - started
        usage = getattr(resp, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        try:
            raw = _anthropic_text(resp.content).strip()
        except IndexError:
            raw = ""
        return CallResult(
            latency_s=latency,
            attempts=attempts,
            retried=attempts > 1,
            stop_reason=getattr(resp, "stop_reason", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            output_text=raw,
            error=None,
        )


def planned_calls(prompts: Sequence[BenchPrompt], arms: Sequence[Arm]) -> list[tuple[int, BenchPrompt, Arm]]:
    """Transitions first, then memory extraction; arm order rotates with the prompt index."""
    ordered = [p for p in prompts if p.prompt_type == "transition"] + [
        p for p in prompts if p.prompt_type == "memory_extract"
    ]
    plan: list[tuple[int, BenchPrompt, Arm]] = []
    order_index = 0
    for index, prompt in enumerate(ordered):
        rotation = index % len(arms)
        for arm in [*arms[rotation:], *arms[:rotation]]:
            plan.append((order_index, prompt, arm))
            order_index += 1
    return plan


def receipt_row(
    *,
    run_id: str,
    arm: Arm,
    mechanism: str,
    prompt: BenchPrompt,
    order_index: int,
    started_at: str,
    result: CallResult,
    quality: dict[str, Any],
    cost_usd: float | None,
) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "call",
        "run_id": run_id,
        "arm": arm.label,
        "model": arm.model,
        "effort": arm.effort,
        "effort_mechanism": mechanism if arm.effort is not None else None,
        "prompt_type": prompt.prompt_type,
        "prompt_id": prompt.prompt_id,
        "prompt_sha256": prompt.prompt_sha256,
        "system_sha256": prompt.system_sha256,
        "order_index": order_index,
        "started_at": started_at,
        "latency_s": round(result.latency_s, 4),
        "attempts": result.attempts,
        "retried": result.retried,
        "stop_reason": result.stop_reason,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "output_chars": len(result.output_text),
        "output_sha256": hashlib.sha256(result.output_text.encode("utf-8")).hexdigest(),
        "output_text": result.output_text,
        "usable": bool(quality.get("usable", False)) and result.error is None,
        "too_long": bool(quality.get("too_long", False)),
        "quality": {k: v for k, v in quality.items() if k not in ("usable", "too_long")},
        "error": result.error,
        "timed_out": result.timed_out,
        "cost_usd": None if cost_usd is None else round(cost_usd, 8),
    }


async def run_live(
    *,
    prompts: Sequence[BenchPrompt],
    arms: Sequence[Arm],
    mechanism: str,
    pricing: dict[str, Pricing],
    receipt_path: Path,
    run_id: str,
    header: dict[str, Any],
    api_key: str,
) -> list[dict[str, Any]]:
    import anthropic

    # The key was resolved by ``live_api_key()`` before any mammamiradio import,
    # so it is the shell's value, never one ``load_dotenv()`` copied from .env.
    client = anthropic.AsyncAnthropic(api_key=api_key)
    create = client.with_options(max_retries=0).messages.create
    rows: list[dict[str, Any]] = []
    plan = planned_calls(prompts, arms)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        handle.flush()
        for order_index, prompt, arm in plan:
            started_at = datetime.now(UTC).isoformat()
            result = await call_arm(create, arm, prompt, mechanism, attempt_timeout(prompt.max_tokens))
            quality = (
                evaluate_output(prompt.prompt_type, result.output_text, result.stop_reason)
                if result.error is None
                else {"usable": False, "too_long": False, "parsed": False}
            )
            cost = (
                pricing[arm.model].cost_usd(result.input_tokens, result.output_tokens) if result.error is None else None
            )
            row = receipt_row(
                run_id=run_id,
                arm=arm,
                mechanism=mechanism,
                prompt=prompt,
                order_index=order_index,
                started_at=started_at,
                result=result,
                quality=quality,
                cost_usd=cost,
            )
            rows.append(row)
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            status = row["error"] or ("ok" if row["usable"] else "unusable")
            print(
                f"[{order_index + 1:3d}/{len(plan)}] {prompt.prompt_id:<14} arm {arm.label} "
                f"{result.latency_s:6.2f}s {status}{' retried' if result.retried else ''}",
                flush=True,
            )
    return rows


# ---------------------------------------------------------------------------
# Summary, gate, report
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile; ``None`` when there is nothing to rank."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def arm_stats(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    completed = [row for row in rows if row["error"] is None]
    latencies = [float(row["latency_s"]) for row in completed]
    costs = [row["cost_usd"] for row in completed]
    priced = [cost for cost in costs if cost is not None]
    return {
        "calls": len(rows),
        "completed": len(completed),
        "errors": len(rows) - len(completed),
        "timeouts": sum(1 for row in rows if row.get("timed_out")),
        "p50_latency_s": percentile(latencies, 0.50),
        "p95_latency_s": percentile(latencies, 0.95),
        "mean_latency_s": statistics.fmean(latencies) if latencies else None,
        "max_latency_s": max(latencies) if latencies else None,
        "usable": sum(1 for row in rows if row["usable"]),
        "not_usable": sum(1 for row in rows if not row["usable"]),
        "too_long": sum(1 for row in rows if row["too_long"]),
        "retried": sum(1 for row in rows if row["retried"]),
        "input_tokens": sum(int(row["input_tokens"]) for row in completed),
        "output_tokens": sum(int(row["output_tokens"]) for row in completed),
        "cost_usd": round(sum(priced), 6) if priced and len(priced) == len(completed) else None,
    }


def gate_verdict(candidate: dict[str, Any], incumbent: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    cand_p95 = candidate["p95_latency_s"]
    inc_p95 = incumbent["p95_latency_s"]
    if cand_p95 is None or inc_p95 is None:
        reasons.append("p95 latency unavailable (no completed calls to compare)")
    elif cand_p95 > LATENCY_HEADROOM * inc_p95:
        reasons.append(f"p95 {cand_p95:.2f}s > {LATENCY_HEADROOM:.2f} x incumbent {inc_p95:.2f}s")
    if candidate["not_usable"] > incumbent["not_usable"]:
        reasons.append(f"not usable {candidate['not_usable']} > incumbent {incumbent['not_usable']}")
    if candidate["too_long"] > incumbent["too_long"]:
        reasons.append(f"too long {candidate['too_long']} > incumbent {incumbent['too_long']}")
    return {"pass": not reasons, "reasons": reasons}


def gate_definition() -> dict[str, Any]:
    return {
        "scope": "per prompt type, arm A is the incumbent",
        "latency": (
            f"p95_latency(X) <= {LATENCY_HEADROOM:.2f} * p95_latency(A), over completed calls"
            " (error rows, timed-out calls included, are excluded from latency)"
        ),
        "p95_method": P95_METHOD,
        "usable": "count(not usable, X) <= count(not usable, A); error rows count as not usable",
        "too_long": "count(too_long, X) <= count(too_long, A)",
        "cost": "reported only, never gated",
        "exit_code": "0 when every candidate arm passes for both prompt types, else 3",
    }


def summarize(rows: Sequence[dict[str, Any]], arms: Sequence[Arm]) -> dict[str, Any]:
    per_type: dict[str, dict[str, Any]] = {}
    verdicts: dict[str, dict[str, Any]] = {}
    for prompt_type in ("transition", "memory_extract"):
        typed = [row for row in rows if row["prompt_type"] == prompt_type]
        stats = {arm.label: arm_stats(row for row in typed if row["arm"] == arm.label) for arm in arms}
        per_type[prompt_type] = stats
        verdicts[prompt_type] = {
            arm.label: gate_verdict(stats[arm.label], stats["A"]) for arm in arms if arm.label != "A"
        }
    all_pass = all(verdict["pass"] for typed in verdicts.values() for verdict in typed.values())
    return {"per_type": per_type, "verdicts": verdicts, "all_pass": all_pass, "exit_code": 0 if all_pass else 3}


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(summary: dict[str, Any], arms: Sequence[Arm], header: dict[str, Any]) -> str:
    timeouts = sum(stats["timeouts"] for typed in summary["per_type"].values() for stats in typed.values())
    documented_default = header["candidate_model_source"].get("documented_default_effort", CANDIDATE_DEFAULT_EFFORT)
    lines = [
        "# Haiku fast-path benchmark",
        "",
        f"Run `{header['run_id']}` started {header['started_at']}; receipts `{header['receipt_path']}`.",
        f"Effort mechanism: `{header['effort_mechanism']}`. Candidate id `{CANDIDATE_MODEL}` confirmed from",
        f"{MODEL_DOCS_URL} (captured {MODEL_DOCS_CAPTURED}); its documented default effort is",
        f"`{documented_default}`.",
        "",
        "Gate: latency only (p95 within +10 % of arm A per prompt type, no more unusable or too-long",
        f"outputs than arm A). p95 method: {header['gate']['p95_method']}. Cost is reported, never gated.",
        f"Latency statistics cover completed calls; the percentiles exclude {timeouts} timed-out",
        "call(s) and every other error row (errors are excluded from latency and counted as not usable).",
        "",
        f"Arms C (effort `{CANDIDATE_DEFAULT_EFFORT}` sent explicitly) and D (no output_config, documented default",
        f"effort `{documented_default}`) are nominally the same server-side configuration, so their spread",
        "doubles as a repeatability estimate.",
        "",
    ]
    for prompt_type, stats in summary["per_type"].items():
        lines.append(f"## {prompt_type}")
        lines.append("")
        lines.append(
            "| Arm | Model | Effort | Calls | Errors | Timeouts | p50 s | p95 s | Mean s | Max s | Not usable |"
            " Too long | Retried | In tok | Out tok | Cost USD | Verdict |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for arm in arms:
            row = stats[arm.label]
            verdict = summary["verdicts"][prompt_type].get(arm.label)
            if verdict is None:
                verdict_text = "incumbent"
            elif verdict["pass"]:
                verdict_text = "PASS"
            else:
                verdict_text = "FAIL: " + "; ".join(verdict["reasons"])
            lines.append(
                f"| {arm.label} | `{arm.model}` | {arm.effort or 'default'} | {row['calls']} | {row['errors']} |"
                f" {row['timeouts']} | {_fmt(row['p50_latency_s'])} | {_fmt(row['p95_latency_s'])} |"
                f" {_fmt(row['mean_latency_s'])} | {_fmt(row['max_latency_s'])} | {row['not_usable']} |"
                f" {row['too_long']} | {row['retried']} | {row['input_tokens']} | {row['output_tokens']} |"
                f" {_fmt(row['cost_usd'], 4)} | {verdict_text} |"
            )
        lines.append("")
    lines.append("## Pricing")
    lines.append("")
    for model, pricing in header["pricing"].items():
        if pricing["input_per_million"] is None:
            lines.append(f"- `{model}`: unpriced ({pricing['source']})")
        else:
            lines.append(
                f"- `{model}`: ${pricing['input_per_million']} in / ${pricing['output_per_million']} out per million"
                f" ({pricing['source']}, {pricing['captured']})"
            )
    lines.append("")
    lines.append(
        f"Overall: {'every candidate arm passed' if summary['all_pass'] else 'at least one arm failed'}"
        f" (exit {summary['exit_code']})."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def _length_stats(values: Sequence[int]) -> str:
    ordered = sorted(values)
    median = ordered[len(ordered) // 2]
    return (
        f"min {ordered[0]} / median {median} / max {ordered[-1]} chars"
        f" (~{ordered[0] // 4} / {median // 4} / {ordered[-1] // 4} tokens at chars/4)"
    )


def print_dry_run(
    prompts: Sequence[BenchPrompt],
    arms: Sequence[Arm],
    mechanism: str,
    pricing: dict[str, Pricing],
    receipt_path: Path,
) -> None:
    print("Haiku fast-path benchmark — dry run (offline, no client constructed, no key read)")
    print()
    for prompt_type in ("transition", "memory_extract"):
        rows = [p for p in prompts if p.prompt_type == prompt_type]
        print(f"{prompt_type}: {len(rows)} prompts, {len({r.prompt_sha256 for r in rows})} distinct")
        print(f"  user prompt   {_length_stats([len(r.prompt) for r in rows])}")
        print(f"  system prompt {_length_stats([len(r.system) for r in rows])}")
        timeout_s = attempt_timeout(rows[0].max_tokens)
        print(f"  caller={rows[0].caller} max_tokens={rows[0].max_tokens} timeout_per_call={timeout_s:.1f}s")
        print(f"  super_italian={sum(1 for r in rows if r.super_italian)} code_path={rows[0].code_path}")
        if prompt_type == "transition":
            hosts = sorted({r.meta["host"] for r in rows})
            styles = sorted({r.meta["style"] for r in rows})
            print(f"  hosts={hosts} styles={styles} recent_texts={sum(1 for r in rows if r.meta['recent_texts'])}")
        else:
            print(
                f"  persona_context={sum(1 for r in rows if r.meta['persona_context'])}"
                f" pinned={sum(1 for r in rows if r.meta['pinned'])}"
                f" lines={min(r.meta['lines'] for r in rows)}-{max(r.meta['lines'] for r in rows)}"
            )
        print()
    print("Arms:")
    for arm in arms:
        effort = f"effort={arm.effort} via {mechanism}" if arm.effort else "no output_config"
        print(f"  {arm.label}  {arm.model:<28} {effort:<32} {arm.description}")
    print(f"Effort mechanism: {mechanism}")
    print()
    plan = planned_calls(prompts, arms)
    print(f"Planned calls: {len(plan)} (sequential, one in flight; arm order rotates with the prompt index)")
    for order_index, prompt, arm in [*plan[:8], *plan[-4:]]:
        print(f"  #{order_index + 1:03d} {prompt.prompt_id:<14} arm {arm.label} max_tokens={prompt.max_tokens}")
        if order_index == 7:
            print("  ...")
    print()
    print("Gate:")
    for key, value in gate_definition().items():
        print(f"  {key}: {value}")
    print()
    print("Pricing (report only):")
    for model, row in pricing.items():
        if row.priced:
            rate = f"${row.input_per_million} in / ${row.output_per_million} out per million"
            print(f"  {model:<28} {rate}  [{row.source}, {row.captured}]")
        else:
            print(f"  {model:<28} unpriced  [{row.source}]")
    print()
    print(f"Receipts would be written to: {receipt_path}")
    print(f"Summary would be written to:  {receipt_path.with_suffix('.summary.json')}")
    print(f"Report would be written to:   {receipt_path.with_suffix('.md')}")
    print()
    print(
        f"Live run: set {BENCH_KEY_ENV} (or {FALLBACK_KEY_ENV}) in the shell environment, not .env,"
        " then run again without --dry-run."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run", action="store_true", help="build and validate the corpus, print the plan, no calls"
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="receipt directory (tmp/evals)")
    parser.add_argument(
        "--prompts-per-type",
        type=int,
        default=PROMPTS_PER_TYPE,
        help=f"live run only: use the first N prompts of each type (1-{PROMPTS_PER_TYPE}) for a cheaper smoke run",
    )
    parser.add_argument(
        "--pricing-capture",
        type=Path,
        default=None,
        help="a fresh local capture of the Anthropic pricing page; an unparseable Haiku 5.5 row leaves it unpriced",
    )
    parser.add_argument("--pricing-capture-date", default=None, help="date label for --pricing-capture (default today)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # Resolved before any mammamiradio import (see live_api_key); the dry run never reads it.
    api_key = "" if args.dry_run else live_api_key()
    if not args.dry_run and not api_key:
        print(
            f"No Anthropic key found: set {BENCH_KEY_ENV} (or {FALLBACK_KEY_ENV}) in the shell environment"
            " and run again. This command reads the shell environment only, not .env, so a key kept in"
            " .env is not seen."
        )
        return 2
    if not 1 <= args.prompts_per_type <= PROMPTS_PER_TYPE:
        print(f"--prompts-per-type must be between 1 and {PROMPTS_PER_TYPE}")
        return 2

    try:
        modes = load_mode_context()
        prompts = [*build_transition_corpus(modes), *build_memory_corpus(modes)]
        validate_corpus(prompts)
        incumbent = incumbent_model()
    except CorpusError as exc:
        print(f"corpus failed validation: {exc}")
        return 2

    arms = build_arms(incumbent)
    mechanism = detect_effort_mechanism()
    pricing = load_pricing(incumbent, args.pricing_capture, args.pricing_capture_date)
    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    receipt_path = args.output_dir / f"haiku-fast-path-{stamp}.jsonl"

    if args.dry_run:
        print_dry_run(prompts, arms, mechanism, pricing, receipt_path)
        return 0

    if args.prompts_per_type < PROMPTS_PER_TYPE:
        keep = args.prompts_per_type
        prompts = [
            *[p for p in prompts if p.prompt_type == "transition"][:keep],
            *[p for p in prompts if p.prompt_type == "memory_extract"][:keep],
        ]

    run_id = f"haiku-fast-path-{stamp}-{uuid.uuid4().hex[:8]}"
    header = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "header",
        "run_id": run_id,
        "started_at": started.isoformat(),
        "receipt_path": _display_path(receipt_path),
        "arms": [asdict(arm) for arm in arms],
        "effort_mechanism": mechanism,
        "candidate_model_source": {
            "url": MODEL_DOCS_URL,
            "captured": MODEL_DOCS_CAPTURED,
            "documented_default_effort": CANDIDATE_DEFAULT_EFFORT,
        },
        "pricing": {model: row.to_dict() for model, row in pricing.items()},
        "gate": gate_definition(),
        "prompts": {
            prompt_type: {
                "count": sum(1 for p in prompts if p.prompt_type == prompt_type),
                "max_tokens": next(p.max_tokens for p in prompts if p.prompt_type == prompt_type),
                "timeout_s": attempt_timeout(next(p.max_tokens for p in prompts if p.prompt_type == prompt_type)),
            }
            for prompt_type in ("transition", "memory_extract")
        },
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    corpus_path = receipt_path.with_suffix(".corpus.json")
    corpus_path.write_text(
        json.dumps(
            [
                {
                    **asdict(prompt),
                    "prompt_sha256": prompt.prompt_sha256,
                    "system_sha256": prompt.system_sha256,
                }
                for prompt in prompts
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    rows = asyncio.run(
        run_live(
            prompts=prompts,
            arms=arms,
            mechanism=mechanism,
            pricing=pricing,
            receipt_path=receipt_path,
            run_id=run_id,
            header=header,
            api_key=api_key,
        )
    )
    summary = summarize(rows, arms)
    summary.update(
        {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "run_id": run_id,
            "started_at": header["started_at"],
            "finished_at": datetime.now(UTC).isoformat(),
            "effort_mechanism": mechanism,
            "arms": header["arms"],
            "pricing": header["pricing"],
            "gate": header["gate"],
            "receipt_path": _display_path(receipt_path),
        }
    )
    receipt_path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    report = render_markdown(summary, arms, header)
    receipt_path.with_suffix(".md").write_text(report, encoding="utf-8")
    print()
    print(report)
    return int(summary["exit_code"])


if __name__ == "__main__":
    sys.exit(main())

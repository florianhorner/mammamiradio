"""Offline contracts for the developer-only banter authoring kit."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from mammamiradio.hosts.scriptwriter import PACKAGED_BANTER_DIRECTION_MAX_CHARS

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "banter-workshop.py"
TEMPLATE = ROOT / "scripts" / "banter-review-board.template.html"
PACK = ROOT / "scripts" / "banter-pack-v1.json"
FEEDBACK = ROOT / "scripts" / "banter-pack-v1-feedback.json"
SPOKEN = ROOT / "mammamiradio" / "assets" / "demo" / "spoken_assets.json"
NODE = shutil.which("node")


def _load_workshop():
    spec = importlib.util.spec_from_file_location("banter_workshop", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True, check=False)


def _mini_report(
    tmp_path: Path,
    *,
    clip_id: str = "01-demo-clip",
    sha256: str | None = None,
    payload: bytes = b"ID3fake-audio",
) -> Path:
    audio = tmp_path / "normal"
    audio.mkdir(parents=True)
    mp3 = audio / f"{clip_id}.mp3"
    mp3.write_bytes(payload)
    digest = sha256 if sha256 is not None else hashlib.sha256(payload).hexdigest()
    report = {
        "schema_version": "1",
        "clips": [
            {
                "id": clip_id,
                "title": "Demo Clip",
                "mode": "normal",
                "variant": "original",
                "context": "evergreen",
                "required_previous_starter_id": None,
                "creative_direction": "Keep Studio B affectionate and timeless.",
                "file": f"normal/{clip_id}.mp3",
                "bytes": len(payload),
                "sha256": digest,
                "duration_seconds": 12.0,
                "transcript": [
                    {"host": "Marco", "text": "The espresso machine filed a complaint."},
                    {"host": "Giulia", "text": "Good. Put it on air."},
                ],
            }
        ],
    }
    path = tmp_path / "review-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _mutate_spoken(tmp_path: Path, clip_id: str, **fields: Any) -> Path:
    spoken = json.loads(SPOKEN.read_text(encoding="utf-8"))
    for entry in spoken["assets"]:
        if Path(entry["path"]).stem == clip_id:
            entry.update(fields)
            break
    path = tmp_path / "spoken.json"
    path.write_text(json.dumps(spoken), encoding="utf-8")
    return path


def test_script_and_pack_artifacts_exist() -> None:
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    feedback = json.loads(FEEDBACK.read_text(encoding="utf-8"))
    assert SCRIPT.is_file() and TEMPLATE.is_file()
    assert len(pack["clips"]) == len(feedback["clips"]) == 21
    assert "Studio B" in pack["description"]


def test_sync_check_passes_against_spoken_assets() -> None:
    result = _run(["sync-check"])
    assert result.returncode == 0, result.stderr
    assert "synchronized" in result.stdout


def test_sync_check_fails_on_hash_and_routing_drift(tmp_path: Path) -> None:
    workshop = _load_workshop()
    feedback = json.loads(FEEDBACK.read_text(encoding="utf-8"))
    feedback["clips"][0]["sha256"] = "0" * 64
    bad = tmp_path / "bad-feedback.json"
    bad.write_text(json.dumps(feedback), encoding="utf-8")
    result = _run(["sync-check", "--feedback", str(bad)])
    assert result.returncode == 1
    assert "does not match" in result.stderr
    with pytest.raises(ValueError, match="spoken mode"):
        workshop.assert_pack_synchronized_with_spoken_assets(
            spoken_path=_mutate_spoken(tmp_path, "01-normal-espresso-machine-union", mode="super_italian")
        )
    with pytest.raises(ValueError, match="spoken predecessor"):
        workshop.assert_pack_synchronized_with_spoken_assets(
            spoken_path=_mutate_spoken(tmp_path, "06-normal-long-time-coming", required_previous_starter_id="")
        )
    with pytest.raises(ValueError, match="spoken special"):
        workshop.assert_pack_synchronized_with_spoken_assets(
            spoken_path=_mutate_spoken(tmp_path, "19-special-other-side", special=False)
        )


def test_sync_check_rejects_missing_feedback_row_and_duplicates(tmp_path: Path) -> None:
    workshop = _load_workshop()
    feedback = json.loads(FEEDBACK.read_text(encoding="utf-8"))
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({**feedback, "clips": feedback["clips"][1:]}), encoding="utf-8")
    with pytest.raises(ValueError, match="plan/feedback id sets diverge"):
        workshop.assert_pack_synchronized_with_spoken_assets(feedback_path=missing)
    duplicate = tmp_path / "dup.json"
    duplicate.write_text(
        json.dumps({**feedback, "clips": [*feedback["clips"], feedback["clips"][0]]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate id"):
        workshop.assert_pack_synchronized_with_spoken_assets(feedback_path=duplicate)


def test_board_hashes_audio_bytes_and_rejects_stale_report(tmp_path: Path) -> None:
    payload = b"ID3actual-bytes"
    report_path = _mini_report(tmp_path, payload=payload, sha256="b" * 64)
    out = tmp_path / "listening-board.html"
    result = _run(["board", "--report", str(report_path), "--output", str(out)])
    assert result.returncode == 1
    assert "does not match audio bytes" in result.stderr
    report_path = _mini_report(tmp_path / "ok", payload=payload)
    out = tmp_path / "ok" / "listening-board.html"
    result = _run(["board", "--report", str(report_path), "--output", str(out)])
    assert result.returncode == 0, result.stderr
    html = out.read_text(encoding="utf-8")
    assert hashlib.sha256(payload).hexdigest() in html
    assert "bbbb" not in html


def test_board_generation_escapes_injection_without_placeholder_substitution(tmp_path: Path) -> None:
    report_path = _mini_report(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["clips"][0]["creative_direction"] = '</script><img src=x onerror="alert(1)"> keep __CONTEXT_AUDIO__ intact'
    report_path.write_text(json.dumps(report), encoding="utf-8")
    out = tmp_path / "listening-board.html"
    result = _run(["board", "--report", str(report_path), "--output", str(out)])
    assert result.returncode == 0, result.stderr
    text = out.read_text(encoding="utf-8")
    assert "__REVIEW_REPORT__" not in text
    assert '<img src=x onerror="alert(1)">' not in text
    assert "\\u003c/script\\u003e" in text
    assert "keep __CONTEXT_AUDIO__ intact" in text
    assert "fonts.googleapis.com" not in text


def test_board_rejects_output_outside_report_directory(tmp_path: Path) -> None:
    report_path = _mini_report(tmp_path / "run")
    out = tmp_path / "review" / "listening-board.html"
    result = _run(["board", "--report", str(report_path), "--output", str(out)])
    assert result.returncode == 1
    assert "report directory" in result.stderr


def test_board_round_trips_feedback_bound_to_audio_hash(tmp_path: Path) -> None:
    payload = b"ID3keep-me"
    digest = hashlib.sha256(payload).hexdigest()
    report_path = _mini_report(tmp_path, payload=payload)
    feedback = {
        "clips": [
            {
                "id": "01-demo-clip",
                "sha256": digest,
                "decision": "keep",
                "notes": "chemistry stays",
                "issues": "bad",
            }
        ]
    }
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text(json.dumps(feedback), encoding="utf-8")
    out = tmp_path / "listening-board.html"
    result = _run(["board", "--report", str(report_path), "--feedback", str(feedback_path), "--output", str(out)])
    assert result.returncode == 0, result.stderr
    html = out.read_text(encoding="utf-8")
    assert "chemistry stays" in html
    stale = _mini_report(tmp_path / "stale", payload=b"ID3other-bytes")
    stale_feedback = tmp_path / "stale" / "feedback.json"
    shutil.copy2(feedback_path, stale_feedback)
    stale_out = tmp_path / "stale" / "listening-board.html"
    result = _run(["board", "--report", str(stale), "--feedback", str(stale_feedback), "--output", str(stale_out)])
    assert result.returncode == 0, result.stderr
    assert "chemistry stays" not in stale_out.read_text(encoding="utf-8")


def test_shipped_audio_board_from_tracked_baseline(tmp_path: Path) -> None:
    out_dir = ROOT / "tmp" / f"banter-accepted-board-test-{tmp_path.name}"
    out = out_dir / "listening-board.html"
    try:
        result = _run(["board", "--from-accepted-baseline", "--shipped-audio", "--output", str(out)])
        assert result.returncode == 0, result.stderr
        html = out.read_text(encoding="utf-8")
        assert "01-normal-espresso-machine-union.mp3" in html
        assert "../../mammamiradio/assets/demo/banter/" in html
        assert not any(out_dir.rglob("01-normal-espresso-machine-union.mp3"))
        assert "cleared: true" in html or "cleared:true" in html
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_render_refuses_nonempty_output(tmp_path: Path) -> None:
    output = tmp_path / "out"
    output.mkdir()
    (output / "stale.txt").write_text("nope", encoding="utf-8")
    result = _run(["render", "--plan", str(PACK), "--output", str(output), "--no-board"])
    assert result.returncode == 1
    assert "not empty" in result.stderr


def test_plan_loader_rejects_unknown_mode_and_overlong_direction(tmp_path: Path) -> None:
    workshop = _load_workshop()
    plan = tmp_path / "bad.json"
    plan.write_text(
        json.dumps([{"id": "bad-row", "mode": "party", "context": "evergreen", "track_id": None, "direction": "nope"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid mode or context"):
        workshop._load_plan(plan, set())
    long_plan = tmp_path / "long.json"
    long_plan.write_text(
        json.dumps(
            [
                {
                    "id": "long-row",
                    "mode": "normal",
                    "context": "evergreen",
                    "track_id": None,
                    "direction": "x" * (PACKAGED_BANTER_DIRECTION_MAX_CHARS + 1),
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="creative direction exceeds"):
        workshop._load_plan(long_plan, set())


def test_packaged_script_safety_helpers() -> None:
    workshop = _load_workshop()
    row: dict[str, Any] = {"context": "evergreen"}
    for text in (
        "See you tonight at Studio B.",
        "Meet us at noon in Studio B.",
        "The rain outside Studio B is brutal.",
        "Ci sentiamo lunedì a Studio B",
    ):
        with pytest.raises(ValueError, match="not timeless"):
            workshop._assert_packaged_script_safe(row, [text])
    exact = {"context": "exact_track"}
    for text in (
        "That saxophone chorus is wonderfully upbeat.",
        "Quel sassofono nel ritornello è bellissimo.",
        "That track sounds like a festival banger.",
    ):
        with pytest.raises(ValueError, match="unprovided audio context"):
            workshop._assert_packaged_script_safe(exact, [text])


def test_context_audio_overwrites_stale_predecessor_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workshop = _load_workshop()
    source = tmp_path / "starter.mp3"
    source.write_bytes(b"NEWBYTES")
    board_dir = tmp_path / "board"
    (board_dir / "context").mkdir(parents=True)
    (board_dir / "context" / "USUAN1100173.mp3").write_bytes(b"OLDBYTES")
    track = SimpleNamespace(provider_track_id="USUAN1100173", display="Long Time Coming", local_path=source)
    monkeypatch.setattr(workshop, "load_starter_tracks", lambda require_complete=True: [track])
    workshop._context_audio_map(
        {"clips": [{"id": "06-x", "context": "exact_track", "required_previous_starter_id": "USUAN1100173"}]},
        board_path=board_dir / "listening-board.html",
        shipped_audio=False,
    )
    assert (board_dir / "context" / "USUAN1100173.mp3").read_bytes() == b"NEWBYTES"


def test_board_html_lifecycle_contract(tmp_path: Path) -> None:
    report_path = _mini_report(tmp_path)
    out = tmp_path / "listening-board.html"
    assert _run(["board", "--report", str(report_path), "--output", str(out)]).returncode == 0
    html = out.read_text(encoding="utf-8")
    assert "cleared: true" in html
    assert "localStorage.removeItem" not in html
    assert "setTimeout" not in html
    assert "pagehide" in html
    assert "try {" in html and "localStorage.setItem" in html
    assert "window.BanterBoard" in html


@pytest.mark.skipif(NODE is None, reason="node is required for executable board lifecycle")
def test_board_state_lifecycle_with_node(tmp_path: Path) -> None:
    payload = b"ID3lifecycle"
    report_path = _mini_report(tmp_path, payload=payload)
    digest = hashlib.sha256(payload).hexdigest()
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text(
        json.dumps({"clips": [{"id": "01-demo-clip", "sha256": digest, "decision": "keep", "notes": "keep me"}]}),
        encoding="utf-8",
    )
    out = tmp_path / "listening-board.html"
    result = _run(["board", "--report", str(report_path), "--feedback", str(feedback_path), "--output", str(out)])
    assert result.returncode == 0
    harness = tmp_path / "lifecycle.js"
    harness.write_text(
        """
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.split("<script>")[1].split("</script>")[0];
function el() {
  return {
    style: {}, innerHTML: "", value: "", hidden: false, textContent: "",
    dataset: {}, addEventListener() {}, querySelector() { return el(); },
    querySelectorAll() { return []; }, setAttribute() {}, click() {},
  };
}
const els = {};
const localStorage = {
  _d: {},
  getItem(k) { return Object.hasOwn(this._d, k) ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
};
const document = {
  getElementById(id) { return (els[id] ||= el()); },
  querySelectorAll() { return []; },
  addEventListener() {},
};
function Audio() {
  this.addEventListener = () => {};
  this.load = () => {};
  this.pause = () => {};
}
const ctx = {
  localStorage, document, console, JSON, Object, Array,
  String, Number, Date, Math, Map, Set, Error, URL, Blob, Audio,
  confirm: () => true, FileReader: function FileReader() {},
  addEventListener() {},
};
ctx.window = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
try {
  vm.runInContext(script + "\\nthis.__board = window.BanterBoard;", ctx);
} catch (err) {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}
const board = ctx.__board;
if (board.getState().clips["01-demo-clip"].decision !== "keep") {
  throw new Error("approved keep missing");
}
board.updateFeedback("01-demo-clip", { notes: "typed note" });
if (board.getState().clips["01-demo-clip"].notes !== "typed note") {
  throw new Error("note not persisted");
}
board.resetBoard();
const key = Object.keys(localStorage._d)[0];
const afterReset = JSON.parse(localStorage.getItem(key));
if (!afterReset.cleared) throw new Error("reset did not write tombstone");
const reloaded = board.loadState();
if (Object.keys(reloaded.clips).length !== 0) {
  throw new Error("reload restored approved after reset");
}
localStorage.setItem = () => { throw new Error("quota"); };
board.persistState();
if (document.getElementById("warn").style.display !== "block") {
  throw new Error("storage failure was not warned");
}
""",
        encoding="utf-8",
    )
    assert NODE is not None
    result = subprocess.run([NODE, str(harness), str(out)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.asyncio
async def test_render_plan_success_retries_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workshop = _load_workshop()
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "id": "01-demo-clip",
                        "title": "Demo",
                        "mode": "normal",
                        "context": "evergreen",
                        "track_id": None,
                        "direction": "Keep it timeless.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"
    attempts = {"n": 0}

    async def fake_render_attempt(base_config, row, starter_by_id, work_dir):
        attempts["n"] += 1
        work_dir.mkdir(parents=True, exist_ok=True)
        if attempts["n"] == 1:
            raise RuntimeError("transient provider failure")
        final_path = work_dir / "final.mp3"
        final_path.write_bytes(b"ID3rendered-audio-bytes")
        return (
            final_path,
            [{"host": "Marco", "text": "The mug is back."}, {"host": "Giulia", "text": "Leave it."}],
            12.5,
        )

    monkeypatch.setattr(workshop, "load_starter_tracks", lambda require_complete=True: [])
    monkeypatch.setattr(workshop, "load_config", lambda _path: MagicMock())
    monkeypatch.setattr(workshop, "_render_attempt", fake_render_attempt)
    monkeypatch.setattr(workshop, "build_listening_board", lambda *args, **kwargs: Path("board.html"))
    report_path = await workshop.render_plan(plan, output, build_board=False)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["clips"][0]["generation_attempt"] == 2
    assert report["clips"][0]["sha256"] == hashlib.sha256(b"ID3rendered-audio-bytes").hexdigest()
    assert (output / "normal" / "01-demo-clip.mp3").is_file()
    assert not (output / "_work").exists()


@pytest.mark.asyncio
async def test_render_attempt_uses_live_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workshop = _load_workshop()
    marco, giulia = SimpleNamespace(name="Marco"), SimpleNamespace(name="Giulia")
    config = MagicMock()
    config.hosts = [marco, giulia]

    async def fake_write_banter(state, cfg, **kwargs):
        assert kwargs["packaged_context"] == "evergreen"
        assert kwargs["require_generated"] is True
        return [
            SimpleNamespace(host=marco, text="The mug is back."),
            SimpleNamespace(host=giulia, text="Leave it."),
        ], None

    async def fake_truth(state, cfg, lines):
        return list(lines), None, False, False

    async def fake_tts(lines, tmp_dir, state=None):
        path = tmp_dir / "dry.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"dry")
        return path

    async def fake_bed(dry_path, cfg, state, prefix=""):
        path = dry_path.parent / "final.mp3"
        path.write_bytes(b"final-audio")
        return path

    def fake_row_config(base, row, work_dir):
        work_dir.mkdir(parents=True, exist_ok=True)
        base.tmp_dir = work_dir / "tmp"
        base.tmp_dir.mkdir(parents=True, exist_ok=True)
        return base

    monkeypatch.setattr(workshop, "write_banter", fake_write_banter)
    monkeypatch.setattr(workshop, "_listener_truth_guard", fake_truth)
    monkeypatch.setattr(workshop, "synthesize_dialogue", fake_tts)
    monkeypatch.setattr(workshop, "validate_segment_audio", lambda *args, **kwargs: None)
    monkeypatch.setattr(workshop, "_apply_and_adopt_talk_bed", fake_bed)
    monkeypatch.setattr(workshop, "probe_duration_sec", lambda path: 12.0)
    monkeypatch.setattr(workshop, "_probe_silence", lambda path: (0.0, 0.0))
    monkeypatch.setattr(workshop, "_probe_volume", lambda path: (-18.0, -1.0))
    monkeypatch.setattr(workshop, "_expected_banter_duration_sec", lambda texts: 8.0)
    monkeypatch.setattr(workshop, "_row_config", fake_row_config)
    row = {
        "id": "01-demo",
        "mode": "normal",
        "context": "evergreen",
        "track_id": None,
        "direction": "Keep it timeless.",
    }
    final, transcript, duration = await workshop._render_attempt(config, row, {}, tmp_path / "work")
    assert final.read_bytes() == b"final-audio"
    assert duration == 12.0
    assert transcript[0]["host"] == "Marco"

    async def timed_copy(state, cfg, **kwargs):
        return [
            SimpleNamespace(host=marco, text="Meet us at noon in Studio B."),
            SimpleNamespace(host=giulia, text="No."),
        ], None

    monkeypatch.setattr(workshop, "write_banter", timed_copy)
    with pytest.raises(ValueError, match="not timeless"):
        await workshop._render_attempt(config, row, {}, tmp_path / "reject")


@pytest.mark.asyncio
async def test_render_plan_exhausted_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workshop = _load_workshop()
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            [
                {
                    "id": "01-demo-clip",
                    "mode": "normal",
                    "context": "evergreen",
                    "track_id": None,
                    "direction": "Keep it timeless.",
                }
            ]
        ),
        encoding="utf-8",
    )

    async def always_fail(*_args, **_kwargs):
        raise RuntimeError("still broken")

    monkeypatch.setattr(workshop, "load_starter_tracks", lambda require_complete=True: [])
    monkeypatch.setattr(workshop, "load_config", lambda _path: MagicMock())
    monkeypatch.setattr(workshop, "_render_attempt", always_fail)
    monkeypatch.setattr(workshop, "MAX_ATTEMPTS", 2)
    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        await workshop.render_plan(plan, tmp_path / "out", build_board=False)

"""Regression guards for capability/setup slow-poll ordering."""

from pathlib import Path

ADMIN_HTML = Path(__file__).parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"


def _slow_poll_block() -> str:
    html = ADMIN_HTML.read_text(encoding="utf-8")
    start = html.index("const SLOW_POLL_DEADLINE_MS")
    end = html.index("async function refresh()", start)
    return html[start:end]


def test_slow_poll_has_a_deadline_and_rejects_invalid_payloads() -> None:
    block = _slow_poll_block()

    assert "const SLOW_POLL_DEADLINE_MS=10000" in block
    assert "const controller=new AbortController()" in block
    assert "signal:controller.signal" in block
    assert "returned an invalid payload" in block
    assert "clearTimeout(deadline)" in block


def test_only_the_newest_slow_poll_can_commit_results() -> None:
    block = _slow_poll_block()

    assert "let _slowPollGeneration=0" in block
    assert "const generation=++_slowPollGeneration" in block
    assert block.count("if(generation!==_slowPollGeneration)return;") >= 5

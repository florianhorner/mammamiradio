"""Tests for the per-segment correlation context.

Verifies the ContextVar collector is shared across asyncio.gather children (so
parallel LLM calls land in one collector) and that reset() prevents a
long-running producer task from leaking a collector into the next segment.
"""

from __future__ import annotations

import asyncio

from mammamiradio.core import provenance_ctx as pc


def test_set_get_reset_roundtrip():
    assert pc.get_collector() is None
    collector = pc.CallCollector(attempt_id="a1")
    token = pc.set_collector(collector)
    assert pc.get_collector() is collector
    pc.reset_collector(token)
    assert pc.get_collector() is None


def test_reset_prevents_leak_into_next_segment():
    c1 = pc.CallCollector(attempt_id="seg1")
    t1 = pc.set_collector(c1)
    pc.reset_collector(t1)
    # A new segment that forgot to set one must see None, not seg1's collector.
    assert pc.get_collector() is None


def test_gather_children_share_one_collector():
    async def _run():
        collector = pc.CallCollector(attempt_id="banter1")
        token = pc.set_collector(collector)
        try:

            async def _call(role: str):
                c = pc.get_collector()
                assert c is collector  # child task inherited the same object
                c.calls.append({"role": role})

            await asyncio.gather(_call("transition"), _call("banter"))
        finally:
            pc.reset_collector(token)
        return collector

    collector = asyncio.run(_run())
    roles = sorted(c["role"] for c in collector.calls)
    assert roles == ["banter", "transition"]

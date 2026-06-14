"""Unit tests for the FM broadcast 'transmitter' chain (egress colouring pass).

These mock ffmpeg — they prove the toggle, the encoding preservation, the
best-effort failure handling, and the SIGABRT-safety SHAPE contract (no stacked
equalizers + loudnorm) without touching real audio. The crash guarantee on real
ffmpeg / Pi aarch64 is covered by the requires_ffmpeg test in
test_normalizer_real_ffmpeg.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from mammamiradio.audio import normalizer
from mammamiradio.audio.normalizer import (
    _broadcast_filter_chain,
    apply_broadcast_chain,
    configure_broadcast_chain,
)


def test_broadcast_disabled_is_noop():
    """A disabled chain never invokes ffmpeg and reports it did nothing."""
    configure_broadcast_chain(False)
    with patch.object(normalizer, "_run_ffmpeg") as m_run:
        assert apply_broadcast_chain(Path("/tmp/in.mp3"), Path("/tmp/out.mp3")) is False
    m_run.assert_not_called()


def test_broadcast_applies_and_returns_true():
    """Enabled: ffmpeg runs with the broadcast filter chain and the station output
    format, and apply reports success."""
    configure_broadcast_chain(True)
    with patch.object(normalizer, "_run_ffmpeg") as m_run:
        assert apply_broadcast_chain(Path("/tmp/in.mp3"), Path("/tmp/out.mp3")) is True
    cmd = m_run.call_args[0][0]
    assert cmd[cmd.index("-filter:a") + 1] == _broadcast_filter_chain()
    # Default house format preserved on the colouring re-encode.
    assert cmd[cmd.index("-b:a") + 1] == "192k"
    assert cmd[cmd.index("-ar") + 1] == "48000"
    assert cmd[cmd.index("-ac") + 1] == "2"


def test_broadcast_preserves_configured_encoding():
    """The colouring re-encode honours a non-default sample rate / channels / bitrate,
    never silently downgrading to the house defaults."""
    configure_broadcast_chain(True, sample_rate=44100, channels=1, bitrate=256)
    with patch.object(normalizer, "_run_ffmpeg") as m_run:
        apply_broadcast_chain(Path("/tmp/in.mp3"), Path("/tmp/out.mp3"))
    cmd = m_run.call_args[0][0]
    assert cmd[cmd.index("-b:a") + 1] == "256k"
    assert cmd[cmd.index("-ar") + 1] == "44100"
    assert cmd[cmd.index("-ac") + 1] == "1"


def test_broadcast_filter_chain_shape_is_sigabrt_safe():
    """The SHAPE contract: NO equalizer and NO loudnorm in the broadcast pass. Three
    equalizers + loudnorm is the psymodel.c:576 SIGABRT on ffmpeg 8.x / Pi aarch64;
    keeping both out of this separate pass is what makes it crash-safe. Values are
    listening-tunable, but this shape must hold."""
    chain = _broadcast_filter_chain()
    assert "equalizer=" not in chain
    assert "loudnorm" not in chain
    # The intended 4-stage transmitter colour is present.
    assert "aphaser=" in chain  # subtle multipath movement (clamped helper)
    assert "treble=" in chain  # gentle pre-emphasis HF shelf
    assert "lowpass=f=15000" in chain  # ~15 kHz FM band-limit
    assert "acompressor=" in chain  # soft broadcast leveller


def test_broadcast_failure_is_best_effort(tmp_path):
    """A failed ffmpeg pass never raises, reports False, and cleans up its tmp output
    so a half-written file is never aired."""
    configure_broadcast_chain(True)
    out = tmp_path / "out.mp3"
    out.write_bytes(b"PARTIAL")  # simulate a half-written output ffmpeg left behind
    with patch.object(normalizer, "_run_ffmpeg", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
        assert apply_broadcast_chain(tmp_path / "in.mp3", out) is False
    assert not out.exists()  # the failed output was removed, never aired


def test_broadcast_holds_norm_sem_during_pass():
    """The extra pass takes a _NORM_SEM slot so it respects the Pi 2-ffmpeg ceiling
    (the regime where the SIGABRT / EQ-count guards live)."""
    configure_broadcast_chain(True)
    held = {}

    def _record(*_args, **_kwargs):
        held["value"] = normalizer._NORM_SEM._value  # 2 when free, 1 while held

    with patch.object(normalizer, "_run_ffmpeg", side_effect=_record):
        apply_broadcast_chain(Path("/tmp/in.mp3"), Path("/tmp/out.mp3"))
    assert held["value"] == 1  # a slot was held across the ffmpeg call


def test_configure_false_after_true_disables():
    """Toggling the chain back off mid-session returns to studio-clean output."""
    configure_broadcast_chain(True)
    configure_broadcast_chain(False)
    with patch.object(normalizer, "_run_ffmpeg") as m_run:
        assert apply_broadcast_chain(Path("/tmp/in.mp3"), Path("/tmp/out.mp3")) is False
    m_run.assert_not_called()

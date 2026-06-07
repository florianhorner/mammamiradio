"""Unit tests for the loudness-reconciliation pass (measure + corrective gain).

These mock ffmpeg — they prove the gain math, the idempotent skip, the clamp,
and the best-effort failure handling without touching real audio. The real
integrated-LUFS guarantee is covered by the requires_ffmpeg tests in
test_normalizer_real_ffmpeg.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from mammamiradio.audio import normalizer
from mammamiradio.audio.normalizer import _reconcile_lufs, configure_loudness_reconcile


def test_reconcile_disabled_is_noop():
    """Default (unconfigured) reconciliation never measures or re-encodes."""
    configure_loudness_reconcile(None, None)
    with (
        patch.object(normalizer, "measure_lufs") as m_measure,
        patch.object(normalizer, "_run_ffmpeg") as m_run,
    ):
        _reconcile_lufs(Path("/tmp/whatever.mp3"))
    m_measure.assert_not_called()
    m_run.assert_not_called()


def test_reconcile_applies_corrective_gain_toward_target():
    """A file below target gets a positive volume bump = target - measured, and the
    re-encode preserves the station output format (sample rate / channels / bitrate)."""
    from mammamiradio.audio.normalizer import _MP3_OUTPUT_ARGS

    configure_loudness_reconcile(-16.0, -15.0)
    with (
        patch.object(normalizer, "measure_lufs", return_value=-22.0),
        patch.object(normalizer, "_run_ffmpeg") as m_run,
        patch("pathlib.Path.replace") as m_replace,
    ):
        _reconcile_lufs(Path("/tmp/seg.mp3"))
    cmd = m_run.call_args[0][0]
    assert "volume=6dB" in cmd  # -16 - (-22) = +6
    for arg in _MP3_OUTPUT_ARGS:
        assert arg in cmd  # reconciled segment must stay in station format
    m_replace.assert_called_once()


def test_reconcile_ad_uses_the_hotter_ad_target():
    """Ads reconcile to ad_target (1 LU hotter), not the main target."""
    configure_loudness_reconcile(-16.0, -15.0)
    with (
        patch.object(normalizer, "measure_lufs", return_value=-20.0),
        patch.object(normalizer, "_run_ffmpeg") as m_run,
        patch("pathlib.Path.replace"),
    ):
        _reconcile_lufs(Path("/tmp/ad.mp3"), ad=True)
    cmd = m_run.call_args[0][0]
    assert "volume=5dB" in cmd  # -15 - (-20) = +5


def test_reconcile_skips_tiny_correction_idempotent():
    """A file already on target (sub-0.5 dB) is not re-encoded — makes the
    redundant terminal passes cost only a measure."""
    configure_loudness_reconcile(-16.0, -15.0)
    with (
        patch.object(normalizer, "measure_lufs", return_value=-16.2),
        patch.object(normalizer, "_run_ffmpeg") as m_run,
    ):
        _reconcile_lufs(Path("/tmp/seg.mp3"))
    m_run.assert_not_called()


def test_reconcile_clamps_huge_gain():
    """A near-silent file is clamped to +12 dB, never pumped unbounded."""
    configure_loudness_reconcile(-16.0, -15.0)
    with (
        patch.object(normalizer, "measure_lufs", return_value=-60.0),
        patch.object(normalizer, "_run_ffmpeg") as m_run,
        patch("pathlib.Path.replace"),
    ):
        _reconcile_lufs(Path("/tmp/quiet.mp3"))
    cmd = m_run.call_args[0][0]
    assert "volume=12dB" in cmd  # clamped from +44


def test_reconcile_measure_failure_is_noop():
    """If measurement fails (None), the file is left as-is and no re-encode runs."""
    configure_loudness_reconcile(-16.0, -15.0)
    with (
        patch.object(normalizer, "measure_lufs", return_value=None),
        patch.object(normalizer, "_run_ffmpeg") as m_run,
    ):
        _reconcile_lufs(Path("/tmp/seg.mp3"))
    m_run.assert_not_called()


def test_reconcile_reencode_failure_keeps_original(tmp_path):
    """A failed re-encode never raises, and the ORIGINAL is left byte-identical — the
    .lufs tmp is the file cleaned up, not the original."""
    configure_loudness_reconcile(-16.0, -15.0)
    original = tmp_path / "seg.mp3"
    original.write_bytes(b"ORIGINAL-AUDIO-BYTES")
    with (
        patch.object(normalizer, "measure_lufs", return_value=-22.0),
        patch.object(normalizer, "_run_ffmpeg", side_effect=subprocess.CalledProcessError(1, "ffmpeg")),
    ):
        _reconcile_lufs(original)  # must not raise
    assert original.read_bytes() == b"ORIGINAL-AUDIO-BYTES"  # untouched
    assert not (tmp_path / "seg.lufs.mp3").exists()  # the tmp, not the original, was cleaned up


def test_reconcile_preserves_configured_encoding():
    """The corrective re-encode honours the station's configured bitrate / sample
    rate / channels, not the house defaults — a non-default config isn't silently
    downgraded by reconcile."""
    configure_loudness_reconcile(-16.0, -15.0, sample_rate=44100, channels=1, bitrate=256)
    with (
        patch.object(normalizer, "measure_lufs", return_value=-22.0),
        patch.object(normalizer, "_run_ffmpeg") as m_run,
        patch("pathlib.Path.replace"),
    ):
        _reconcile_lufs(Path("/tmp/seg.mp3"))
    cmd = m_run.call_args[0][0]
    assert cmd[cmd.index("-b:a") + 1] == "256k"
    assert cmd[cmd.index("-ar") + 1] == "44100"
    assert cmd[cmd.index("-ac") + 1] == "1"


def test_reconcile_partial_none_config_disables():
    """A partial config (exactly one target None) must DISABLE reconcile entirely,
    not half-enable it — guards the `main is None or ad is None` branch."""
    for main_t, ad_t in [(-16.0, None), (None, -15.0)]:
        configure_loudness_reconcile(main_t, ad_t)
        with patch.object(normalizer, "measure_lufs") as m_measure:
            _reconcile_lufs(Path("/tmp/x.mp3"))
            _reconcile_lufs(Path("/tmp/x.mp3"), ad=True)
        m_measure.assert_not_called()

"""Real-ffmpeg integration tests for the normalizer filter chain.

These tests call actual ffmpeg — no subprocess mocks. They exist to catch
crashes (SIGABRT, non-zero exits) that mocked tests cannot detect, such as
the psymodel.c:576 SIGABRT triggered by 3 equalizers + loudnorm on ffmpeg 8.x
aarch64.

Run by the pi-smoke CI job on ubuntu-24.04-arm to exercise the filter chain
on real ARM hardware with the system ffmpeg.

Skipped automatically when ffmpeg is not in PATH.
"""

from __future__ import annotations

import subprocess

import pytest

from mammamiradio.audio.normalizer import normalize

# Use the project-wide `requires_ffmpeg` marker (registered in pyproject.toml)
# instead of skipif. The default `addopts = "-m 'not requires_ffmpeg'"` means
# these tests aren't even collected on default runs — no skip overhead. The
# pi-smoke.yml workflow opts back in with `-m requires_ffmpeg`.
pytestmark = pytest.mark.requires_ffmpeg


def _make_silent_mp3(path) -> None:
    """Generate a 3-second silent MP3 using ffmpeg lavfi source."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "3",
            "-acodec",
            "libmp3lame",
            "-ab",
            "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_tone_mp3(path, duration_sec: float = 2.0, freq: int = 440) -> None:
    """Generate a constant-amplitude sine tone — used to verify fade-in shapes."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={duration_sec}:sample_rate=44100",
            "-ac",
            "2",
            "-acodec",
            "libmp3lame",
            "-ab",
            "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_noise_mp3(path, duration_sec: float = 3.0) -> None:
    """Generate pink noise — a realistic broadband signal ebur128 can integrate.

    A pure sine reads at the -70 LUFS gate floor and cannot be loudness-measured,
    so loudness assertions need a broadband source.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=d={duration_sec}:c=pink:a=0.5",
            "-ac",
            "2",
            "-acodec",
            "libmp3lame",
            "-ab",
            "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _measure_rms(path, start_sec: float, window_sec: float) -> float:
    """Return the RMS volume (linear, 0-1) of an mp3 window via ffmpeg volumedetect."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-ss",
            f"{start_sec}",
            "-i",
            str(path),
            "-t",
            f"{window_sec}",
            "-filter:a",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
    )
    stderr = result.stderr.decode(errors="ignore")
    # volumedetect reports "mean_volume: -X.X dB"
    import re

    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", stderr)
    if not m:
        return 0.0
    db = float(m.group(1))
    return 10 ** (db / 20.0)


def test_normalize_music_eq_chain_does_not_crash_real_ffmpeg(tmp_path):
    """The music_eq filter chain must not crash ffmpeg (no SIGABRT, exit 0).

    Three equalizers + loudnorm trigger psymodel.c:576 SIGABRT in ffmpeg 8.x
    on aarch64. This test exercises the real filter chain on real ffmpeg to
    catch that crash class before it reaches production Pi/HA Green installs.
    """
    src = tmp_path / "input.mp3"
    out = tmp_path / "output.mp3"
    _make_silent_mp3(src)

    # Must not raise — a SIGABRT or ffmpeg error raises CalledProcessError
    normalize(src, out, loudnorm=True, music_eq=True)

    assert out.exists(), "normalize() produced no output file"
    assert out.stat().st_size > 0, "normalize() produced an empty output file"


def test_normalize_no_music_eq_does_not_crash_real_ffmpeg(tmp_path):
    """The standard loudnorm-only path must also complete without error."""
    src = tmp_path / "input.mp3"
    out = tmp_path / "output.mp3"
    _make_silent_mp3(src)

    normalize(src, out, loudnorm=True, music_eq=False)

    assert out.exists()
    assert out.stat().st_size > 0


def test_normalize_applies_soft_fade_in_on_real_audio(tmp_path):
    """End-to-end: a constant-amplitude tone normalized via the final-output path
    must show a measurable amplitude ramp in the first 250ms, so music→voice
    hand-offs in the stream aren't hard cuts.

    Guards against the 2026-04-21 regression Florian flagged ("the fade overs
    from song to speakers arent soft there is a noticable drop").
    """
    src = tmp_path / "tone.mp3"
    out = tmp_path / "tone_norm.mp3"
    _make_tone_mp3(src, duration_sec=2.0)

    normalize(src, out, loudnorm=True, music_eq=False)
    assert out.exists() and out.stat().st_size > 0

    # First 100ms should be significantly quieter than mid-track.
    head_rms = _measure_rms(out, start_sec=0.0, window_sec=0.1)
    mid_rms = _measure_rms(out, start_sec=1.0, window_sec=0.2)
    # fade is linear over 250ms; first 100ms averages ~20% of target amplitude
    # (rough — allow wide margin for ffmpeg encoding + loudnorm variability).
    assert mid_rms > 0, "mid-track RMS should be non-zero on a steady tone"
    assert head_rms < mid_rms * 0.6, (
        f"first 100ms ({head_rms:.4f}) should be noticeably quieter than mid-track ({mid_rms:.4f}) due to 250ms fade-in"
    )


def _measure_lufs_real(path) -> float | None:
    """Integrated LUFS of an mp3 via ffmpeg ebur128 (real measurement)."""
    import re

    result = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
        check=False,
        capture_output=True,
    )
    # Last match = the end-of-stream Summary value, not the per-frame -70.0 floor.
    matches = re.findall(r"I:\s+(-?\d+\.\d+)\s+LUFS", result.stderr.decode(errors="ignore"))
    return float(matches[-1]) if matches else None


def test_loudness_reconcile_brings_music_to_target_real_ffmpeg(tmp_path):
    """With reconciliation enabled, a finished segment lands within +/-2 LU of the
    main target regardless of its raw level — the loudness-unification contract."""
    from mammamiradio.audio.normalizer import configure_loudness_reconcile

    src = tmp_path / "input.mp3"
    out = tmp_path / "output.mp3"
    _make_noise_mp3(src, duration_sec=3.0)
    # Precondition: the raw source sits well outside the +/-1.5 LU fast-path skip
    # window around -16, so the full loudnorm path runs (guards a future signal drift).
    raw = _measure_lufs_real(src)
    assert raw is not None and raw < -18.0, f"test signal {raw} LUFS too close to -16 skip window"

    # A NON-default target so reconcile MUST do real work: the loudnorm path lands
    # near -16 on its own, so a -16 target would pass even with reconcile disabled.
    target = -20.0
    try:
        configure_loudness_reconcile(target, -15.0)
        normalize(src, out, loudnorm=True, music_eq=False)
    finally:
        configure_loudness_reconcile(None, None)

    assert out.exists() and out.stat().st_size > 0
    lufs = _measure_lufs_real(out)
    assert lufs is not None, "could not measure output LUFS"
    assert abs(lufs - target) <= 2.0, f"reconciled music at {lufs} LUFS, expected {target} +/-2"
    assert lufs < -18.0, "reconcile did not run (output near the -16 loudnorm level, not the -20 target)"


def test_loudness_reconcile_brings_ad_to_hotter_target_real_ffmpeg(tmp_path):
    """Ads reconcile to the (1 LU hotter) ad target so they pop without the old
    jarring 2-LU jump above the music floor."""
    from mammamiradio.audio.normalizer import configure_loudness_reconcile, normalize_ad

    src = tmp_path / "ad_in.mp3"
    out = tmp_path / "ad_out.mp3"
    _make_noise_mp3(src, duration_sec=3.0)

    # Distinct ad target far from the main target so a main/ad mixup fails — this
    # proves normalize_ad reconciles to ad_lufs_target (cfg[1]), not the main one.
    main_target, ad_target = -20.0, -12.0
    try:
        configure_loudness_reconcile(main_target, ad_target)
        normalize_ad(src, out)
    finally:
        configure_loudness_reconcile(None, None)

    assert out.exists() and out.stat().st_size > 0
    lufs = _measure_lufs_real(out)
    assert lufs is not None, "could not measure ad output LUFS"
    assert abs(lufs - ad_target) <= 2.0, f"reconciled ad at {lufs} LUFS, expected ad target {ad_target} +/-2"


def test_mix_voice_with_bed_does_not_crash_real_ffmpeg(tmp_path):
    """mix_voice_with_bed (amix + loudnorm) must complete on real ffmpeg — it was
    previously uncovered by the SIGABRT smoke (only normalize() was)."""
    from mammamiradio.audio.normalizer import mix_voice_with_bed

    voice = tmp_path / "voice.mp3"
    bed = tmp_path / "bed.mp3"
    out = tmp_path / "bedded.mp3"
    _make_tone_mp3(voice, duration_sec=2.0, freq=330)
    _make_tone_mp3(bed, duration_sec=4.0, freq=110)

    mix_voice_with_bed(voice, bed, out)

    assert out.exists() and out.stat().st_size > 0


def test_loudness_reconcile_pulls_dynaudnorm_output_to_target_real_ffmpeg(tmp_path):
    """The slice's headline contract on the Green: the addon dynaudnorm path has no
    fixed integrated target on its own, but reconcile pulls it to lufs_target. Uses a
    NON-default target so the -16-centered fast-path skip can't accidentally pass it."""
    from types import SimpleNamespace

    from mammamiradio.audio.normalizer import configure_loudness_reconcile

    src = tmp_path / "in.mp3"
    out = tmp_path / "out.mp3"
    _make_noise_mp3(src, duration_sec=3.0)
    addon_cfg = SimpleNamespace(
        is_addon=True,
        audio=SimpleNamespace(sample_rate=48000, channels=2, bitrate=192),
    )
    target = -14.0  # non-default
    try:
        configure_loudness_reconcile(target, -13.0)
        normalize(src, out, addon_cfg, loudnorm=True, music_eq=False)
    finally:
        configure_loudness_reconcile(None, None)

    lufs = _measure_lufs_real(out)
    assert lufs is not None
    assert abs(lufs - target) <= 2.0, f"dynaudnorm output reconciled to {lufs}, expected {target} +/-2"


def test_loudness_reconcile_runs_after_fast_path_skip_real_ffmpeg(tmp_path, caplog):
    """A track already ~-16 hits the fast-path skip (full loudnorm bypassed), but a
    non-default target must still be reached — reconcile runs on the skipped path
    (regression guard for the skip-bypasses-reconcile bug under a non-default target)."""
    from mammamiradio.audio.normalizer import configure_loudness_reconcile

    pre = tmp_path / "pre.mp3"
    near16 = tmp_path / "near16.mp3"
    _make_noise_mp3(pre, duration_sec=3.0)
    normalize(pre, near16, loudnorm=True, music_eq=False)  # reconcile off -> ~-16 via loudnorm
    pre_lufs = _measure_lufs_real(near16)
    assert pre_lufs is not None and abs(pre_lufs - (-16.0)) <= 1.5, (
        f"precondition: input must be in the skip window, got {pre_lufs}"
    )

    out = tmp_path / "out.mp3"
    target = -13.0  # non-default: the bug was the -16 skip bypassing reconcile entirely
    try:
        configure_loudness_reconcile(target, -12.0)
        with caplog.at_level("INFO", logger="mammamiradio.audio.normalizer"):
            normalize(near16, out, loudnorm=True, music_eq=False)  # input ~-16 -> skip fires
    finally:
        configure_loudness_reconcile(None, None)

    # Prove the fast-path skip actually fired (else the test passes via the full
    # loudnorm path and never exercises the skip-then-reconcile interaction).
    assert "LUFS skip" in caplog.text, "fast-path skip did not fire — test signal drifted out of the window"
    assert "LUFS reconcile" in caplog.text, "reconcile did not run on the skipped path"
    lufs = _measure_lufs_real(out)
    assert lufs is not None
    assert abs(lufs - target) <= 2.0, f"skipped-path output at {lufs}, expected {target} +/-2 (reconcile bypassed?)"

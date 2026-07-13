from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from mammamiradio.core.config import StationConfig
from mammamiradio.core.models import HostPersonality
from scripts import generate_welcome_clips as gen

FAKE_MP3_BYTES = b"fake mp3" * 200
ORIGINAL_MP3_BYTES = b"original-good" * 128
REGENERATED_MP3_BYTES = b"regenerated" * 128


@pytest.fixture(autouse=True)
def _loud_by_default(monkeypatch):
    """Treat every rendered clip as real speech by default.

    The generator probes each output's peak level to reject the TTS silence
    fallback; stubbing it keeps the generation tests off ffmpeg/volumedetect.
    The silence test overrides this with a floor-level reading.
    """
    monkeypatch.setattr(gen, "_probe_volume", lambda _path: (-18.0, -3.0))
    monkeypatch.setattr(gen, "_probe_duration_sec", lambda _path: 2.0)


def _welcome_config() -> StationConfig:
    """Small source-of-truth host roster used by the free Edge generator."""
    return cast(
        StationConfig,
        SimpleNamespace(
            hosts=[
                HostPersonality(
                    name="Marco",
                    voice="paid-marco-voice",
                    style="host",
                    engine="elevenlabs",
                    edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
                ),
                HostPersonality(
                    name="Giulia",
                    voice="it-IT-IsabellaNeural",
                    style="host",
                    engine="edge",
                ),
            ]
        ),
    )


@pytest.fixture
def configured_clips() -> tuple[gen.WelcomeClip, ...]:
    return gen.resolve_welcome_clips(_welcome_config())


def test_welcome_clip_contract_is_italian_and_well_formed() -> None:
    """The contract must stay non-empty, .mp3-named, and free of empty text/host.

    The runtime globs welcome/*.mp3, so a clip with a non-mp3 name or blank text
    would silently never air. This guards the shape the generator promises.
    """
    assert gen.WELCOME_CLIPS, "welcome clip contract must not be empty"
    names = [clip.filename for clip in gen.WELCOME_CLIPS]
    assert len(names) == len(set(names)), "welcome clip filenames must be unique"
    for clip in gen.WELCOME_CLIPS:
        assert clip.filename.endswith(".mp3")
        assert clip.text.strip()
        assert clip.host_name in {"Marco", "Giulia"}
        assert not clip.voice, "voice IDs must come from the configured host"


def test_resolve_welcome_clips_uses_configured_edge_or_cloud_fallback() -> None:
    by_host = {clip.host_name: clip.voice for clip in gen.resolve_welcome_clips(_welcome_config())}

    assert by_host == {
        "Marco": "it-IT-GiuseppeMultilingualNeural",
        "Giulia": "it-IT-IsabellaNeural",
    }


@pytest.mark.asyncio
async def test_generate_clips_writes_each_clip_via_tts(tmp_path, monkeypatch, configured_clips) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        calls.append((text, voice, engine))
        output_path.write_bytes(FAKE_MP3_BYTES)
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", fake_synthesize)

    results = await gen.generate_clips(configured_clips, tmp_path)

    assert [r.status for r in results] == [gen.STATUS_GENERATED] * len(configured_clips)
    assert len(calls) == len(configured_clips)
    # Every clip lands on disk under its declared filename, and the result
    # reports the exact path it wrote to.
    for result in results:
        assert result.output_path == tmp_path / result.clip.filename
        assert result.output_path.read_bytes() == FAKE_MP3_BYTES
    # Always rendered through Edge — the contract voices are Edge voice IDs.
    assert all(engine == "edge" for _, _, engine in calls)


@pytest.mark.asyncio
async def test_generate_clips_skips_existing_unless_overwrite(tmp_path, monkeypatch, configured_clips) -> None:
    calls: list[str] = []

    async def fake_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        calls.append(output_path.name)
        output_path.write_bytes(REGENERATED_MP3_BYTES)
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", fake_synthesize)

    # Pre-seed one clip so it is already present.
    existing = configured_clips[0]
    (tmp_path / existing.filename).write_bytes(ORIGINAL_MP3_BYTES)

    skipped = await gen.generate_clips(configured_clips, tmp_path)
    by_name = {r.clip.filename: r for r in skipped}
    assert by_name[existing.filename].status == gen.STATUS_SKIPPED
    assert existing.filename not in calls
    assert (tmp_path / existing.filename).read_bytes() == ORIGINAL_MP3_BYTES

    # --overwrite rebuilds everything, including the pre-existing clip.
    rebuilt = await gen.generate_clips(configured_clips, tmp_path, overwrite=True)
    assert len(rebuilt) == len(configured_clips)
    assert all(r.status == gen.STATUS_GENERATED for r in rebuilt)
    assert (tmp_path / existing.filename).read_bytes() == REGENERATED_MP3_BYTES


@pytest.mark.asyncio
async def test_overwrite_failure_preserves_existing_clip(tmp_path, monkeypatch, configured_clips) -> None:
    """A failed rebuild must not delete the last known-good committed clip."""

    existing = configured_clips[0]
    dest = tmp_path / existing.filename
    dest.write_bytes(ORIGINAL_MP3_BYTES)

    async def always_fail(text, voice, output_path, *, engine="edge", **kwargs):
        output_path.write_bytes(b"partial")
        raise RuntimeError("voice unavailable")

    monkeypatch.setattr(gen.tts_module, "synthesize", always_fail)

    results = await gen.generate_clips((existing,), tmp_path, overwrite=True)

    assert results[0].status == gen.STATUS_FAILED
    assert dest.read_bytes() == ORIGINAL_MP3_BYTES
    # The known-good clip is all that survives — no staging leftover beside it.
    assert list(tmp_path.iterdir()) == [dest]


@pytest.mark.asyncio
async def test_intermediates_never_land_in_globbed_clip_dir(tmp_path, monkeypatch, configured_clips) -> None:
    """No partial or raw render may surface under the runtime-globbed clip dir.

    The playback loop serves any ``*.mp3`` directly under welcome/ (Path.glob
    matches dotfiles too), and the real ``synthesize`` writes a sibling
    ``.raw.mp3`` next to its target. Both must stay in the staging subdir, so an
    interrupted generation can never leave a servable partial/un-normalized clip
    where the station would pick it up.
    """
    mid_run_globs: list[list[str]] = []

    async def fake_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        # Mirror the real edge path: drop a sibling raw file, then the output.
        raw = output_path.with_suffix(".raw.mp3")
        raw.write_bytes(FAKE_MP3_BYTES)
        output_path.write_bytes(FAKE_MP3_BYTES)
        # Capture what the runtime glob would see while a render is in flight.
        mid_run_globs.append([p.name for p in tmp_path.glob("*.mp3")])
        raw.unlink(missing_ok=True)
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", fake_synthesize)

    results = await gen.generate_clips(configured_clips, tmp_path)

    assert all(r.status == gen.STATUS_GENERATED for r in results)
    # Mid-generation, the clip dir's glob exposes ONLY already-published contract
    # clips — never a staging/raw/tmp intermediate, under any naming scheme.
    contract_names = {clip.filename for clip in configured_clips}
    for names in mid_run_globs:
        assert set(names) <= contract_names, f"non-published artifact in globbed dir: {names}"
    # Final state: only the published clips, and the staging dir is gone.
    assert sorted(p.name for p in tmp_path.glob("*.mp3")) == sorted(c.filename for c in configured_clips)
    assert not (tmp_path / gen.STAGING_DIRNAME).exists()


@pytest.mark.asyncio
async def test_generate_clips_dry_run_writes_nothing(tmp_path, monkeypatch, configured_clips) -> None:
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dry-run must not synthesize")

    monkeypatch.setattr(gen.tts_module, "synthesize", fail_if_called)

    results = await gen.generate_clips(configured_clips, tmp_path, dry_run=True)

    assert len(results) == len(configured_clips)
    assert all(r.status == gen.STATUS_PLANNED for r in results)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_generate_clips_rejects_short_nonsilent_render(tmp_path, monkeypatch, configured_clips) -> None:
    """A loud but truncated render must not be accepted as a welcome clip."""

    async def fake_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        output_path.write_bytes(FAKE_MP3_BYTES)
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", fake_synthesize)
    monkeypatch.setattr(gen, "_probe_duration_sec", lambda _path: 0.1)

    results = await gen.generate_clips(configured_clips, tmp_path)

    assert len(results) == len(configured_clips)
    assert all(r.status == gen.STATUS_FAILED for r in results)
    assert all("too short" in r.error for r in results)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_dry_run_reports_existing_clips_as_skipped(tmp_path, monkeypatch, configured_clips) -> None:
    """A preview must reflect what a real run would do: existing clips are skipped.

    Without the existence check running before the dry-run short-circuit, an
    already-present clip would be previewed as 'planned' even though a real run
    would skip it — a misleading preview.
    """

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dry-run must not synthesize")

    monkeypatch.setattr(gen.tts_module, "synthesize", fail_if_called)
    existing = configured_clips[0]
    (tmp_path / existing.filename).write_bytes(b"already here")

    results = await gen.generate_clips(configured_clips, tmp_path, dry_run=True)
    by_name = {r.clip.filename: r for r in results}

    assert len(results) == len(configured_clips)
    assert by_name[existing.filename].status == gen.STATUS_SKIPPED
    # The rest are previewed as planned; the dry run writes nothing new.
    planned = [r for r in results if r.clip.filename != existing.filename]
    assert all(r.status == gen.STATUS_PLANNED for r in planned)
    assert list(tmp_path.iterdir()) == [tmp_path / existing.filename]


@pytest.mark.asyncio
async def test_generate_clips_one_failure_does_not_abort_batch(tmp_path, monkeypatch, configured_clips) -> None:
    fail_for = configured_clips[0]

    async def flaky_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        if text == fail_for.text:
            raise RuntimeError("voice unavailable")
        output_path.write_bytes(FAKE_MP3_BYTES)
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", flaky_synthesize)

    results = await gen.generate_clips(configured_clips, tmp_path)
    by_name = {r.clip.filename: r for r in results}

    assert len(results) == len(configured_clips)
    assert by_name[fail_for.filename].status == gen.STATUS_FAILED
    assert by_name[fail_for.filename].error == "voice unavailable"
    # Every other clip still got written despite the one failure.
    others = [r for r in results if r.clip.filename != fail_for.filename]
    assert all(r.status == gen.STATUS_GENERATED for r in others)


@pytest.mark.asyncio
async def test_generate_clips_rejects_silent_tts_fallback(tmp_path, monkeypatch, configured_clips) -> None:
    """synthesize() returns silence (not an error) when the voice backend is down.

    The generator must treat that as a failure and discard the file, so an
    operator never commits a silent welcome greeting.
    """

    async def fake_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        output_path.write_bytes(FAKE_MP3_BYTES)
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", fake_synthesize)
    # Simulate the silence fallback: every rendered file measures near the floor.
    monkeypatch.setattr(gen, "_probe_volume", lambda _path: (-91.0, -91.0))

    results = await gen.generate_clips(configured_clips, tmp_path)

    assert len(results) == len(configured_clips)
    assert all(r.status == gen.STATUS_FAILED for r in results)
    assert all("silence" in r.error for r in results)
    # Silent files are discarded, not left on disk for an operator to commit.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_failed_render_partial_artifact_is_removed(tmp_path, monkeypatch, configured_clips) -> None:
    """A synth failure after a partial write must not leave a corrupt file behind.

    Otherwise the next default run would see it via dest.exists() and report the
    corrupt artifact as 'skipped' instead of regenerating it.
    """

    async def synth_then_fail(text, voice, output_path, *, engine="edge", **kwargs):
        output_path.write_bytes(b"partial")  # opened/wrote before the pipeline failed
        raise RuntimeError("normalize timed out")

    monkeypatch.setattr(gen.tts_module, "synthesize", synth_then_fail)

    results = await gen.generate_clips(configured_clips, tmp_path)

    assert len(results) == len(configured_clips)
    assert all(r.status == gen.STATUS_FAILED for r in results)
    # No partial files survive to be mis-reported as "skipped" on a rerun.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_generate_clips_rejects_fallback_voice_substitution(tmp_path, monkeypatch, configured_clips) -> None:
    """synthesize() silently swaps in a default voice when the requested one fails.

    A non-silent render in the wrong speaker (Giulia's line in a male voice) must
    be rejected, not shipped as the contract voice.
    """

    async def fake_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        output_path.write_bytes(FAKE_MP3_BYTES)
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", fake_synthesize)
    # Every requested voice has failed this session → synthesize would substitute
    # the default Edge fallback voice. (_loud_by_default keeps it non-silent.)
    failed = {clip.voice for clip in configured_clips}
    monkeypatch.setattr(gen.tts_module, "_failed_edge_voices", failed)

    results = await gen.generate_clips(configured_clips, tmp_path)

    assert len(results) == len(configured_clips)
    assert all(r.status == gen.STATUS_FAILED for r in results)
    assert all("fallback voice" in r.error for r in results)
    # Wrong-speaker renders are discarded, not left for an operator to commit.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_silent_clip_cleanup_failure_is_recorded_not_raised(tmp_path, monkeypatch, configured_clips) -> None:
    """If discarding a silent clip fails, the batch records it and keeps going.

    A locked/permission-denied unlink must not escape and abort the remaining
    clips; the failure note is folded into the result instead.
    """

    async def fake_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        output_path.write_bytes(FAKE_MP3_BYTES)
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", fake_synthesize)
    monkeypatch.setattr(gen, "_probe_volume", lambda _path: (-91.0, -91.0))

    def boom(*_args, **_kwargs):
        raise OSError("file locked")

    monkeypatch.setattr(gen.Path, "unlink", boom)

    results = await gen.generate_clips(configured_clips, tmp_path)

    assert len(results) == len(configured_clips)
    assert all(r.status == gen.STATUS_FAILED for r in results)
    assert all("silence" in r.error for r in results)
    assert all("could not delete" in r.error for r in results)


@pytest.mark.asyncio
async def test_generate_clips_handles_unwritable_output_dir(tmp_path, monkeypatch, configured_clips) -> None:
    """A filesystem error creating the output dir is recorded, not raised.

    Placing a regular file where the output directory should be makes
    mkdir(exist_ok=True) raise — the batch must record failures and continue
    rather than crashing with a traceback.
    """

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("synthesize must not run when the output dir can't be made")

    monkeypatch.setattr(gen.tts_module, "synthesize", fail_if_called)
    blocker = tmp_path / "welcome"
    blocker.write_text("i am a file, not a directory")

    results = await gen.generate_clips(configured_clips, blocker)

    assert len(results) == len(configured_clips)
    assert all(r.status == gen.STATUS_FAILED for r in results)


def test_main_dry_run_returns_zero_and_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dry-run must not synthesize")

    monkeypatch.setattr(gen.tts_module, "synthesize", fail_if_called)
    monkeypatch.setattr(gen, "load_config", lambda _path: _welcome_config())

    rc = gen.main(["--dry-run", "--output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 0
    assert "planned" in captured.out
    assert list(tmp_path.iterdir()) == []


def test_main_returns_nonzero_when_a_clip_fails(tmp_path, monkeypatch) -> None:
    async def always_fail(*_args, **_kwargs):
        raise RuntimeError("no engine")

    monkeypatch.setattr(gen.tts_module, "synthesize", always_fail)
    monkeypatch.setattr(gen, "load_config", lambda _path: _welcome_config())

    rc = gen.main(["--output-dir", str(tmp_path)])

    assert rc == 1

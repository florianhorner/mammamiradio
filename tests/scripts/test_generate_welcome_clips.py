from __future__ import annotations

import pytest

from scripts import generate_welcome_clips as gen


def test_welcome_clip_contract_is_italian_and_well_formed() -> None:
    """The contract must stay non-empty, .mp3-named, and free of empty text/voice.

    The runtime globs welcome/*.mp3, so a clip with a non-mp3 name or blank text
    would silently never air. This guards the shape the generator promises.
    """
    assert gen.WELCOME_CLIPS, "welcome clip contract must not be empty"
    names = [clip.filename for clip in gen.WELCOME_CLIPS]
    assert len(names) == len(set(names)), "welcome clip filenames must be unique"
    for clip in gen.WELCOME_CLIPS:
        assert clip.filename.endswith(".mp3")
        assert clip.text.strip()
        assert clip.voice.startswith("it-IT-"), "welcome clips are Italian-only by design"


@pytest.mark.asyncio
async def test_generate_clips_writes_each_clip_via_tts(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        calls.append((text, voice, engine))
        output_path.write_bytes(b"fake mp3")
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", fake_synthesize)

    results = await gen.generate_clips(gen.WELCOME_CLIPS, tmp_path, engine="edge")

    assert [r.status for r in results] == [gen.STATUS_GENERATED] * len(gen.WELCOME_CLIPS)
    assert len(calls) == len(gen.WELCOME_CLIPS)
    # Every clip lands on disk under its declared filename, and the result
    # reports the exact path it wrote to.
    for result in results:
        assert result.output_path == tmp_path / result.clip.filename
        assert result.output_path.read_bytes() == b"fake mp3"
    # Engine choice is forwarded to the TTS pipeline.
    assert all(engine == "edge" for _, _, engine in calls)


@pytest.mark.asyncio
async def test_generate_clips_skips_existing_unless_overwrite(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        calls.append(output_path.name)
        output_path.write_bytes(b"regenerated")
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", fake_synthesize)

    # Pre-seed one clip so it is already present.
    existing = gen.WELCOME_CLIPS[0]
    (tmp_path / existing.filename).write_bytes(b"original")

    skipped = await gen.generate_clips(gen.WELCOME_CLIPS, tmp_path)
    by_name = {r.clip.filename: r for r in skipped}
    assert by_name[existing.filename].status == gen.STATUS_SKIPPED
    assert existing.filename not in calls
    assert (tmp_path / existing.filename).read_bytes() == b"original"

    # --overwrite rebuilds everything, including the pre-existing clip.
    rebuilt = await gen.generate_clips(gen.WELCOME_CLIPS, tmp_path, overwrite=True)
    assert all(r.status == gen.STATUS_GENERATED for r in rebuilt)
    assert (tmp_path / existing.filename).read_bytes() == b"regenerated"


@pytest.mark.asyncio
async def test_generate_clips_dry_run_writes_nothing(tmp_path, monkeypatch) -> None:
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dry-run must not synthesize")

    monkeypatch.setattr(gen.tts_module, "synthesize", fail_if_called)

    results = await gen.generate_clips(gen.WELCOME_CLIPS, tmp_path, dry_run=True)

    assert all(r.status == gen.STATUS_PLANNED for r in results)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_generate_clips_one_failure_does_not_abort_batch(tmp_path, monkeypatch) -> None:
    fail_for = gen.WELCOME_CLIPS[0].filename

    async def flaky_synthesize(text, voice, output_path, *, engine="edge", **kwargs):
        if output_path.name == fail_for:
            raise RuntimeError("voice unavailable")
        output_path.write_bytes(b"fake mp3")
        return output_path

    monkeypatch.setattr(gen.tts_module, "synthesize", flaky_synthesize)

    results = await gen.generate_clips(gen.WELCOME_CLIPS, tmp_path)
    by_name = {r.clip.filename: r for r in results}

    assert by_name[fail_for].status == gen.STATUS_FAILED
    assert by_name[fail_for].error == "voice unavailable"
    # Every other clip still got written despite the one failure.
    others = [r for r in results if r.clip.filename != fail_for]
    assert all(r.status == gen.STATUS_GENERATED for r in others)


def test_main_dry_run_returns_zero_and_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dry-run must not synthesize")

    monkeypatch.setattr(gen.tts_module, "synthesize", fail_if_called)

    rc = gen.main(["--dry-run", "--output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 0
    assert "planned" in captured.out
    assert list(tmp_path.iterdir()) == []


def test_main_returns_nonzero_when_a_clip_fails(tmp_path, monkeypatch) -> None:
    async def always_fail(*_args, **_kwargs):
        raise RuntimeError("no engine")

    monkeypatch.setattr(gen.tts_module, "synthesize", always_fail)

    rc = gen.main(["--output-dir", str(tmp_path)])

    assert rc == 1

"""Focused safety and mix-contract tests for schema-v2 imaging recipes."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.audio.imaging import ImagingLibrary, reserve_unique_recipe_foreground_sources
from mammamiradio.audio.normalizer import fit_audio_oneshot, generate_tone, loop_audio_bed, mix_oneshot_layers


def _asset(asset_id: str, path: str, *, kind: str = "cue") -> dict[str, object]:
    return {
        "id": asset_id,
        "path": path,
        "kind": kind,
        "tags": ["italian-night", kind],
        "source_ids": [f"source:{asset_id}"],
    }


def _write_manifest(assets_dir: Path, assets: list[dict[str, object]], recipes: list[dict[str, object]]) -> None:
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "manifest.json").write_text(
        json.dumps({"schema_version": 2, "assets": assets, "recipes": recipes}),
        encoding="utf-8",
    )


def _touch_assets(assets_dir: Path, *paths: str) -> None:
    for relative_path in paths:
        path = assets_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"dry-cue")


def test_resolve_ad_recipe_returns_safe_cached_concrete_paths(tmp_path: Path) -> None:
    assets_dir = tmp_path / "imaging"
    _touch_assets(assets_dir, "beds/caffe.mp3", "sfx/applause.mp3", "sfx/trumpet.mp3")
    _write_manifest(
        assets_dir,
        [
            _asset("caffe-bed", "beds/caffe.mp3", kind="bed"),
            _asset("applause", "sfx/applause.mp3"),
            _asset("trumpet", "sfx/trumpet.mp3"),
        ],
        [
            {
                "id": "late-night-win",
                "bed": {"asset_id": "caffe-bed", "gain_db": -17.5},
                "cues": [
                    {"anchor": "after_first_voice", "asset_id": "applause", "gain_db": -8.0, "max_duration_sec": 0.8},
                    {"anchor": "outro", "asset_id": "trumpet", "gain_db": -10.0, "max_duration_sec": 0.6},
                ],
            }
        ],
    )
    library = ImagingLibrary([523], tmp_path, assets_dir=assets_dir)

    resolved = library.resolve_ad_recipe("late-night-win", variant_key="ad-42")
    assert resolved is not None
    assert resolved.bed_path == assets_dir / "beds/caffe.mp3"
    assert resolved.bed_gain_db == -17.5
    assert [(cue.anchor, cue.asset_path.name, cue.gain_db, cue.max_duration_sec) for cue in resolved.cues] == [
        ("after_first_voice", "applause.mp3", -8.0, 0.8),
        ("outro", "trumpet.mp3", -10.0, 0.6),
    ]
    assert resolved.cues[0].asset_kind == "cue"
    assert resolved.cues[0].source_ids == ("source:applause",)
    assert resolved.cues[0].recorded_foreground_source_ids == ("source:applause",)
    assert resolved.oneshots == resolved.cues

    # A second lookup avoids parsing/selecting again while the manifest is unchanged.
    assert library.resolve_ad_recipe("late-night-win", variant_key="ad-42") is resolved


def test_unique_foreground_reservation_uses_layer_roles_and_preserves_bed(tmp_path: Path) -> None:
    assets_dir = tmp_path / "imaging"
    _touch_assets(assets_dir, "beds/room.mp3", "sfx/first.mp3", "sfx/conflict.mp3", "sfx/fresh.mp3")
    first = _asset("first", "sfx/first.mp3")
    first["source_ids"] = ["shared", "first-texture"]
    first["layers"] = [
        {"source_id": "shared", "role": "foreground"},
        {"source_id": "first-texture", "role": "texture"},
    ]
    conflict = _asset("conflict", "sfx/conflict.mp3")
    conflict["source_ids"] = ["shared"]
    conflict["layers"] = [{"source_id": "shared", "role": "foreground"}]
    fresh = _asset("fresh", "sfx/fresh.mp3")
    fresh["source_ids"] = ["fresh", "shared"]
    fresh["layers"] = [
        {"source_id": "fresh", "role": "foreground"},
        {"source_id": "shared", "role": "texture"},
    ]
    _write_manifest(
        assets_dir,
        [_asset("room", "beds/room.mp3", kind="bed"), first, conflict, fresh],
        [
            {
                "id": "first",
                "cues": [{"anchor": "intro", "asset_id": "first", "gain_db": -9, "max_duration_sec": 0.5}],
            },
            {
                "id": "later",
                "bed": {"asset_id": "room", "gain_db": -22},
                "cues": [
                    {"anchor": "intro", "asset_id": "conflict", "gain_db": -9, "max_duration_sec": 0.5},
                    {"anchor": "outro", "asset_id": "fresh", "gain_db": -11, "max_duration_sec": 0.5},
                ],
            },
        ],
    )
    library = ImagingLibrary([523], tmp_path, assets_dir=assets_dir)
    first_recipe = library.resolve_ad_recipe("first")
    later_recipe = library.resolve_ad_recipe("later")
    assert first_recipe is not None
    assert later_recipe is not None

    reserved: set[str] = set()
    first_guarded, first_suppressed = reserve_unique_recipe_foreground_sources(first_recipe, reserved)
    later_guarded, later_suppressed = reserve_unique_recipe_foreground_sources(later_recipe, reserved)

    assert first_guarded is first_recipe
    assert first_suppressed == ()
    assert reserved == {"shared", "fresh"}
    assert later_guarded.bed_path == assets_dir / "beds/room.mp3"
    assert [cue.asset_path.name for cue in later_guarded.cues] == ["fresh.mp3"]
    assert [cue.asset_path.name for cue in later_suppressed] == ["conflict.mp3"]


def test_incomplete_layer_coverage_falls_back_to_all_declared_sources(tmp_path: Path) -> None:
    """An omitted foreground layer cannot bypass conservative source reservation."""
    assets_dir = tmp_path / "imaging"
    _touch_assets(assets_dir, "sfx/incomplete.mp3")
    incomplete = _asset("incomplete", "sfx/incomplete.mp3")
    incomplete["source_ids"] = ["shared", "texture"]
    incomplete["layers"] = [{"source_id": "texture", "role": "texture"}]
    _write_manifest(
        assets_dir,
        [incomplete],
        [
            {
                "id": "incomplete-recipe",
                "cues": [{"anchor": "intro", "asset_id": "incomplete", "gain_db": -9, "max_duration_sec": 0.5}],
            }
        ],
    )
    library = ImagingLibrary([523], tmp_path, assets_dir=assets_dir)
    recipe = library.resolve_ad_recipe("incomplete-recipe")
    assert recipe is not None
    assert recipe.cues[0].recorded_foreground_source_ids == ("shared", "texture")

    guarded, suppressed = reserve_unique_recipe_foreground_sources(recipe, {"shared"})

    assert guarded.cues == ()
    assert suppressed == recipe.cues


def test_core_break_foregrounds_seed_recipe_reservations(tmp_path: Path) -> None:
    assets_dir = tmp_path / "imaging"
    _touch_assets(
        assets_dir,
        "bumpers/ad_in.mp3",
        "bumpers/ad_mid.mp3",
        "bumpers/ad_out.mp3",
        "stingers/music_ad.mp3",
        "stingers/ad_music.mp3",
        "stingers/music_to_speech.mp3",
        "stingers/speech_to_music.mp3",
    )
    assets: list[dict[str, object]] = []
    for asset_id, path, source_id in (
        ("ad-in", "bumpers/ad_in.mp3", "radio"),
        ("ad-mid", "bumpers/ad_mid.mp3", "epiano"),
        ("ad-out", "bumpers/ad_out.mp3", "tape"),
        ("music-ad", "stingers/music_ad.mp3", "specific-music-ad"),
        ("ad-music", "stingers/ad_music.mp3", "specific-ad-music"),
        ("m2s", "stingers/music_to_speech.mp3", "generated-m2s"),
        ("s2m", "stingers/speech_to_music.mp3", "generated-s2m"),
    ):
        asset = _asset(asset_id, path, kind="transition")
        asset["source_ids"] = [source_id]
        asset["layers"] = [{"source_id": source_id, "role": "foreground"}]
        assets.append(asset)
    _write_manifest(assets_dir, assets, [])
    library = ImagingLibrary([523], tmp_path, assets_dir=assets_dir)

    assert library.core_break_foreground_source_ids() == (
        "radio",
        "epiano",
        "tape",
        "specific-music-ad",
        "specific-ad-music",
        "generated-m2s",
        "generated-s2m",
    )


def test_resolver_allows_a_cue_only_recipe_but_rejects_missing_or_escaped_assets(tmp_path: Path) -> None:
    assets_dir = tmp_path / "imaging"
    _touch_assets(assets_dir, "sfx/laugh.mp3")
    _write_manifest(
        assets_dir,
        [_asset("laugh", "sfx/laugh.mp3")],
        [
            {
                "id": "cue-only",
                "cues": [{"anchor": "after_first_voice", "asset_id": "laugh", "gain_db": -9, "max_duration_sec": 0.7}],
            }
        ],
    )
    library = ImagingLibrary([523], tmp_path, assets_dir=assets_dir)

    resolved = library.resolve_ad_recipe("cue-only")
    assert resolved is not None
    assert resolved.bed_path is None

    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"not-packaged")
    _write_manifest(
        assets_dir,
        [_asset("escape", "../outside.mp3")],
        [{"id": "unsafe", "cues": []}],
    )
    assert library.resolve_ad_recipe("unsafe") is None


def test_resolver_allows_a_bed_only_recipe(tmp_path: Path) -> None:
    assets_dir = tmp_path / "imaging"
    _touch_assets(assets_dir, "beds/quiet-room.mp3")
    _write_manifest(
        assets_dir,
        [_asset("quiet-room", "beds/quiet-room.mp3", kind="bed")],
        [{"id": "quiet-break", "bed": {"asset_id": "quiet-room", "gain_db": -21.0}}],
    )

    resolved = ImagingLibrary([523], tmp_path, assets_dir=assets_dir).resolve_ad_recipe("quiet-break")
    assert resolved is not None
    assert resolved.bed_path == assets_dir / "beds/quiet-room.mp3"
    assert resolved.cues == ()


def test_missing_asset_only_disables_its_dependent_recipe_and_can_recover(tmp_path: Path) -> None:
    """A damaged optional operator file must not take healthy recipes down with it."""
    assets_dir = tmp_path / "imaging"
    _touch_assets(assets_dir, "sfx/healthy.mp3")
    _write_manifest(
        assets_dir,
        [
            _asset("healthy", "sfx/healthy.mp3"),
            _asset("missing", "sfx/missing.mp3"),
        ],
        [
            {
                "id": "healthy-recipe",
                "cues": [{"anchor": "mid", "asset_id": "healthy", "gain_db": -8, "max_duration_sec": 0.2}],
            },
            {
                "id": "missing-recipe",
                "cues": [{"anchor": "mid", "asset_id": "missing", "gain_db": -8, "max_duration_sec": 0.2}],
            },
        ],
    )
    library = ImagingLibrary([523], tmp_path, assets_dir=assets_dir)

    assert library.resolve_ad_recipe("healthy-recipe") is not None
    assert library.resolve_ad_recipe("missing-recipe") is None

    _touch_assets(assets_dir, "sfx/missing.mp3")
    assert library.resolve_ad_recipe("missing-recipe") is not None


@pytest.mark.parametrize(
    "recipe",
    [
        {
            "id": "too-many-cues",
            "cues": [
                {"anchor": "intro", "asset_id": "cue", "gain_db": -8, "max_duration_sec": 0.2},
                {"anchor": "mid", "asset_id": "cue", "gain_db": -8, "max_duration_sec": 0.2},
                {"anchor": "outro", "asset_id": "cue", "gain_db": -8, "max_duration_sec": 0.2},
            ],
        },
        {
            "id": "invalid-reference",
            "cues": [{"anchor": "mid", "asset_id": "missing", "gain_db": -8, "max_duration_sec": 0.2}],
        },
        {
            "id": "unknown-anchor",
            "cues": [{"anchor": "after_hook", "asset_id": "cue", "gain_db": -8, "max_duration_sec": 0.2}],
        },
        {
            "id": "legacy-bed-candidates",
            "bed_candidates": ["cue"],
            "cues": [{"anchor": "mid", "asset_id": "cue", "gain_db": -8, "max_duration_sec": 0.2}],
        },
    ],
)
def test_resolver_rejects_invalid_recipe_shapes_without_raising(tmp_path: Path, recipe: dict[str, object]) -> None:
    assets_dir = tmp_path / "imaging"
    _touch_assets(assets_dir, "sfx/cue.mp3")
    _write_manifest(assets_dir, [_asset("cue", "sfx/cue.mp3")], [recipe])

    assert ImagingLibrary([523], tmp_path, assets_dir=assets_dir).resolve_ad_recipe(str(recipe["id"])) is None


def test_mix_oneshot_layers_uses_one_unmastered_ffmpeg_pass(tmp_path: Path) -> None:
    base = tmp_path / "base.mp3"
    laugh = tmp_path / "laugh.mp3"
    trumpet = tmp_path / "trumpet.mp3"
    output = tmp_path / "mixed.mp3"

    with patch("mammamiradio.audio.normalizer._run_ffmpeg") as run_ffmpeg:
        assert (
            mix_oneshot_layers(
                base,
                [(laugh, 0.25, -8.0, 0.7), (trumpet, 1.5, -11.5, 0.4)],
                output,
            )
            == output
        )

    run_ffmpeg.assert_called_once()
    command = run_ffmpeg.call_args.args[0]
    assert command.count("-i") == 3
    filter_complex = command[command.index("-filter_complex") + 1]
    assert "adelay=250:all=1" in filter_complex
    assert "adelay=1500:all=1" in filter_complex
    assert "amix=inputs=3:duration=first:normalize=0" in filter_complex
    assert "loudnorm" not in filter_complex


def test_mix_oneshot_layers_refuses_more_than_two_cues(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at most two"):
        mix_oneshot_layers(
            tmp_path / "base.mp3",
            [
                (tmp_path / "one.mp3", 0.0, -8.0, 0.2),
                (tmp_path / "two.mp3", 0.0, -8.0, 0.2),
                (tmp_path / "three.mp3", 0.0, -8.0, 0.2),
            ],
            tmp_path / "output.mp3",
        )


def test_mix_oneshot_layers_rejects_same_path_and_invalid_layer_values(tmp_path: Path) -> None:
    base = tmp_path / "base.mp3"
    base.write_bytes(b"base")
    cue = tmp_path / "cue.mp3"
    cue.write_bytes(b"cue")

    with pytest.raises(ValueError, match="distinct output path"):
        mix_oneshot_layers(base, [(cue, 0.0, -8.0, 0.2)], base)
    with pytest.raises(ValueError, match="must be numeric"):
        mix_oneshot_layers(base, [(cue, "late", -8.0, 0.2)], tmp_path / "out.mp3")  # type: ignore[list-item]
    with pytest.raises(ValueError, match="must be finite"):
        mix_oneshot_layers(base, [(cue, float("nan"), -8.0, 0.2)], tmp_path / "out.mp3")
    with pytest.raises(ValueError, match="non-negative"):
        mix_oneshot_layers(base, [(cue, -0.1, -8.0, 0.2)], tmp_path / "out.mp3")


def test_mix_oneshot_layers_copies_base_when_no_cues(tmp_path: Path) -> None:
    base = tmp_path / "base.mp3"
    output = tmp_path / "out.mp3"
    base.write_bytes(b"night-drive-base")

    assert mix_oneshot_layers(base, (), output) == output
    assert output.read_bytes() == b"night-drive-base"


def test_loop_audio_bed_uses_stream_loop_and_loudnorm(tmp_path: Path) -> None:
    source = tmp_path / "casa_notte.mp3"
    output = tmp_path / "looped.mp3"
    source.write_bytes(b"night-drive-bed")

    with patch("mammamiradio.audio.normalizer._run_ffmpeg") as run_ffmpeg:
        assert loop_audio_bed(source, output, 4.0, fade_out_sec=0.4) == output

    run_ffmpeg.assert_called_once()
    command = run_ffmpeg.call_args.args[0]
    assert command[command.index("-stream_loop") + 1] == "-1"
    audio_filter = command[command.index("-af") + 1]
    assert "loudnorm=I=-18" in audio_filter
    assert "afade=t=out" in audio_filter


def test_fit_audio_oneshot_silence_pads_without_repeating_source(tmp_path: Path) -> None:
    source = tmp_path / "ad_mid.mp3"
    output = tmp_path / "fitted.mp3"
    source.write_bytes(b"single-note")

    with patch("mammamiradio.audio.normalizer._run_ffmpeg") as run_ffmpeg:
        assert fit_audio_oneshot(source, output, 0.8, fade_out_sec=0.08) == output

    command = run_ffmpeg.call_args.args[0]
    assert "-stream_loop" not in command
    audio_filter = command[command.index("-af") + 1]
    assert "apad=whole_dur=0.8" in audio_filter
    assert "atrim=0:0.8" in audio_filter
    assert "loudnorm=I=-16" in audio_filter


@pytest.mark.requires_ffmpeg
def test_fit_audio_oneshot_has_no_second_attack_in_padded_tail(tmp_path: Path) -> None:
    source = tmp_path / "single-note.mp3"
    output = tmp_path / "fitted.mp3"
    generate_tone(source, freq_hz=784, duration_sec=0.35)

    fit_audio_oneshot(source, output, 0.8, fade_out_sec=0.08)

    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-ss",
            "0.55",
            "-i",
            str(output),
            "-t",
            "0.15",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", completed.stderr)
    assert match, completed.stderr
    assert float(match.group(1)) <= -70.0


@pytest.mark.requires_ffmpeg
def test_mix_oneshot_layers_outputs_a_single_bed_length_render(tmp_path: Path) -> None:
    base = tmp_path / "base.mp3"
    cue = tmp_path / "cue.mp3"
    output = tmp_path / "mixed.mp3"
    generate_tone(base, freq_hz=220, duration_sec=1.2)
    generate_tone(cue, freq_hz=880, duration_sec=0.4)

    mix_oneshot_layers(base, [(cue, 0.5, -6.0, 0.25)], output)

    duration = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(output),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    assert float(duration.stdout) == pytest.approx(1.2, abs=0.08)


@pytest.mark.requires_ffmpeg
def test_mix_oneshot_layers_keeps_the_base_level_stable_across_delayed_cues(tmp_path: Path) -> None:
    """Delayed dry cues may add energy, but must never re-scale the base render."""
    base = tmp_path / "base.mp3"
    cue = tmp_path / "cue.mp3"
    output = tmp_path / "mixed.mp3"
    generate_tone(base, freq_hz=220, duration_sec=4.0)
    generate_tone(cue, freq_hz=880, duration_sec=0.4)
    mix_oneshot_layers(base, [(cue, 1.2, -24.0, 0.2)], output)

    def base_level(start_sec: float) -> float:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-ss",
                str(start_sec),
                "-t",
                "0.35",
                "-i",
                str(output),
                "-af",
                "lowpass=f=400,volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=True,
            text=True,
        )
        match = re.findall(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", completed.stderr)
        assert match, completed.stderr
        return float(match[-1])

    before = base_level(0.4)
    after = base_level(2.0)
    assert after == pytest.approx(before, abs=0.75)

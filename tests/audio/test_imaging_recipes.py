"""Focused safety and mix-contract tests for schema-v2 imaging recipes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.audio.imaging import ImagingLibrary
from mammamiradio.audio.normalizer import generate_tone, mix_oneshot_layers


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
                    {"anchor": "after_hook", "asset_id": "applause", "gain_db": -8.0, "max_duration_sec": 0.8},
                    {"anchor": "before_tag", "asset_id": "trumpet", "gain_db": -10.0, "max_duration_sec": 0.6},
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
        ("after_hook", "applause.mp3", -8.0, 0.8),
        ("before_tag", "trumpet.mp3", -10.0, 0.6),
    ]
    assert resolved.oneshots == resolved.cues

    # A second lookup avoids parsing/selecting again while the manifest is unchanged.
    assert library.resolve_ad_recipe("late-night-win", variant_key="ad-42") is resolved


def test_resolver_allows_a_cue_only_recipe_but_rejects_missing_or_escaped_assets(tmp_path: Path) -> None:
    assets_dir = tmp_path / "imaging"
    _touch_assets(assets_dir, "sfx/laugh.mp3")
    _write_manifest(
        assets_dir,
        [_asset("laugh", "sfx/laugh.mp3")],
        [
            {
                "id": "cue-only",
                "cues": [{"anchor": "after_hook", "asset_id": "laugh", "gain_db": -9, "max_duration_sec": 0.7}],
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


@pytest.mark.parametrize(
    "recipe",
    [
        {
            "id": "too-many-cues",
            "cues": [
                {"anchor": "a", "asset_id": "cue", "gain_db": -8, "max_duration_sec": 0.2},
                {"anchor": "b", "asset_id": "cue", "gain_db": -8, "max_duration_sec": 0.2},
                {"anchor": "c", "asset_id": "cue", "gain_db": -8, "max_duration_sec": 0.2},
            ],
        },
        {
            "id": "invalid-reference",
            "cues": [{"anchor": "a", "asset_id": "missing", "gain_db": -8, "max_duration_sec": 0.2}],
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
    assert "amix=inputs=3:duration=first" in filter_complex
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

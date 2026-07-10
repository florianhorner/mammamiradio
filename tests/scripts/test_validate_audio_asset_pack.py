"""Independent contract tests for the public audio-pack provenance gate."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts import validate_audio_asset_pack as validator


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _format() -> dict[str, int | str]:
    return {
        "codec": "mp3",
        "sample_rate_hz": 48_000,
        "channels": 2,
        "bitrate_kbps": 192,
    }


def _source(source_id: str = "applause_master", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": source_id,
        "license": "CC0-1.0",
        "source_url": f"https://example.test/sounds/{source_id}",
        "source_sha256": "a" * 64,
        "creator": "Example performer",
        "title": "A dry applause recording",
        "modification": "Trimmed, normalized, and rendered into the station mix.",
    }
    payload.update(overrides)
    return payload


def _asset(pack_dir: Path, asset_id: str, path: str, source_ids: list[str], **overrides: object) -> dict[str, object]:
    content = f"independent fixture audio:{asset_id}".encode()
    destination = pack_dir / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    payload: dict[str, object] = {
        "id": asset_id,
        "path": path,
        "kind": "bed" if "/beds/" in f"/{path}" else "cue",
        "tags": ["fixture", "audio"],
        "source_ids": source_ids,
        "sha256": _sha256(content),
        "format": _format(),
        "duration_target_sec": 1.0,
    }
    payload.update(overrides)
    return payload


def _manifest(pack_dir: Path, **overrides: object) -> dict[str, object]:
    applause = _asset(pack_dir, "applause_short", "sfx/applause_short.mp3", ["applause_master"])
    bed = _asset(pack_dir, "night_bed", "beds/night_bed.mp3", ["applause_master"], duration_target_sec=8.0)
    payload: dict[str, object] = {
        "schema_version": 2,
        "pack": "Fixture public radio pack",
        "sources": [_source()],
        "assets": [applause, bed],
        "recipes": [
            {
                "id": "ad_break",
                "bed": {"asset_id": "night_bed", "gain_db": -19.0},
                "cues": [
                    {
                        "anchor": "intro",
                        "asset_id": "applause_short",
                        "gain_db": -4.0,
                        "max_duration_sec": 0.75,
                    }
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


def _write_manifest(pack_dir: Path, payload: dict[str, object]) -> None:
    (pack_dir / "manifest.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def fake_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep test packs independent of FFmpeg and of checked-in audio assets."""

    def probe(path: Path) -> validator.AudioProbe:
        duration = 8.0 if path.name == "night_bed.mp3" else 1.0
        return validator.AudioProbe("mp3", 48_000, 2, 192.0, duration)

    monkeypatch.setattr(validator, "_probe_audio", probe)


def test_valid_public_pack_allows_missing_local_original_and_generates_stable_attribution(
    tmp_path: Path, fake_probe: None
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    _write_manifest(pack_dir, _manifest(pack_dir))

    report = validator.validate_audio_asset_pack(pack_dir)
    validator.write_attribution(report)
    first = report.attribution_path.read_text(encoding="utf-8")
    validator.check_attribution(report)
    validator.write_attribution(report)

    assert "# Audio asset attribution" in first
    assert "Example performer" in first
    assert "CC0 1.0 Universal" in first
    assert "`applause_short`" in first
    assert report.attribution_path.read_text(encoding="utf-8") == first


def test_cc_by_requires_explicit_attribution_and_modification(tmp_path: Path, fake_probe: None) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    sources = payload["sources"]
    assert isinstance(sources, list)
    source = sources[0]
    assert isinstance(source, dict)
    source["license"] = "CC-BY-4.0"
    source.pop("modification")
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError) as caught:
        validator.validate_audio_asset_pack(pack_dir)

    message = str(caught.value)
    assert "sources[0].attribution is required for CC-BY-4.0 sources" in message
    assert "sources[0].modification must be a non-empty string" in message


def test_source_original_is_optional_but_is_hash_checked_when_supplied(tmp_path: Path, fake_probe: None) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    original = tmp_path / "curator" / "applause.wav"
    original.parent.mkdir()
    original.write_bytes(b"archived original")
    payload = _manifest(pack_dir)
    sources = payload["sources"]
    assert isinstance(sources, list)
    source = sources[0]
    assert isinstance(source, dict)
    source["original_path"] = str(original)
    source["source_sha256"] = "b" * 64
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError, match="original_path SHA-256 differs"):
        validator.validate_audio_asset_pack(pack_dir)


def test_asset_output_hash_and_format_are_checked(tmp_path: Path, fake_probe: None) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    assets = payload["assets"]
    assert isinstance(assets, list)
    asset = assets[0]
    assert isinstance(asset, dict)
    asset["sha256"] = "c" * 64
    asset["format"] = {**_format(), "sample_rate_hz": 44_100}
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError) as caught:
        validator.validate_audio_asset_pack(pack_dir)

    message = str(caught.value)
    assert "asset 'applause_short' SHA-256 differs from manifest" in message
    assert "sample rate is 48000Hz, expected 44100Hz" in message


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("kind", "assets[0].kind must be a non-empty string"),
        ("tags", "assets[0].tags must be a list"),
    ],
)
def test_asset_metadata_required_by_runtime_is_rejected_by_validator(
    tmp_path: Path, fake_probe: None, field: str, message: str
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    assets = payload["assets"]
    assert isinstance(assets, list)
    asset = assets[0]
    assert isinstance(asset, dict)
    asset.pop(field)
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError, match=re.escape(message)):
        validator.validate_audio_asset_pack(pack_dir)


def test_recipes_resolve_asset_references_and_cue_duration_bounds(tmp_path: Path, fake_probe: None) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    recipes = payload["recipes"]
    assert isinstance(recipes, list)
    recipe = recipes[0]
    assert isinstance(recipe, dict)
    cues = recipe["cues"]
    assert isinstance(cues, list)
    cue = cues[0]
    assert isinstance(cue, dict)
    cue["asset_id"] = "missing_asset"
    cue["max_duration_sec"] = 2.0
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError) as caught:
        validator.validate_audio_asset_pack(pack_dir)

    message = str(caught.value)
    assert "recipes[0].cues[0].asset_id references undeclared asset 'missing_asset'" in message
    # The invalid reference cannot yield a trustworthy duration bound, so keep
    # a second valid cue to prove the bound check itself works.
    cue["asset_id"] = "applause_short"
    _write_manifest(pack_dir, payload)
    with pytest.raises(validator.AudioAssetPackValidationError, match="exceeds asset 'applause_short' duration bound"):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize(
    ("recipe", "message"),
    [
        (
            {
                "id": "too-many-cues",
                "cues": [
                    {"anchor": "intro", "asset_id": "applause_short", "gain_db": -8, "max_duration_sec": 0.2},
                    {"anchor": "mid", "asset_id": "applause_short", "gain_db": -8, "max_duration_sec": 0.2},
                    {"anchor": "outro", "asset_id": "applause_short", "gain_db": -8, "max_duration_sec": 0.2},
                ],
            },
            "must contain at most 2 entries",
        ),
        (
            {
                "id": "unknown-anchor",
                "cues": [
                    {"anchor": "after_hook", "asset_id": "applause_short", "gain_db": -8, "max_duration_sec": 0.2}
                ],
            },
            "anchor must be one of",
        ),
        (
            {
                "id": "legacy-bed-candidates",
                "bed_candidates": ["night_bed"],
                "cues": [{"anchor": "mid", "asset_id": "applause_short", "gain_db": -8, "max_duration_sec": 0.2}],
            },
            "contains unsupported field",
        ),
    ],
)
def test_recipe_schema_rejects_runtime_unsupported_shapes(
    tmp_path: Path,
    fake_probe: None,
    recipe: dict[str, object],
    message: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir, recipes=[recipe])
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError, match=message):
        validator.validate_audio_asset_pack(pack_dir)


def test_legacy_asset_sources_alias_and_cli_attribution_check_are_supported(
    tmp_path: Path, fake_probe: None, capsys: pytest.CaptureFixture[str]
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    assets = payload["assets"]
    assert isinstance(assets, list)
    for asset in assets:
        assert isinstance(asset, dict)
        asset["sources"] = asset.pop("source_ids")
    _write_manifest(pack_dir, payload)

    assert validator.main(["--pack-dir", str(pack_dir), "--write-attribution"]) == 0
    assert "Wrote attribution" in capsys.readouterr().out
    assert validator.main(["--pack-dir", str(pack_dir)]) == 0
    assert "Audio asset pack OK: 1 sources, 2 assets, 1 recipes" in capsys.readouterr().out


def test_cli_reports_stale_attribution_without_rewriting_it(
    tmp_path: Path, fake_probe: None, capsys: pytest.CaptureFixture[str]
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    _write_manifest(pack_dir, _manifest(pack_dir))

    assert validator.main(["--pack-dir", str(pack_dir), "--write-attribution"]) == 0
    attribution = pack_dir / "ATTRIBUTION.md"
    attribution.write_text("stale\n", encoding="utf-8")
    assert validator.main(["--pack-dir", str(pack_dir)]) == 2

    assert "attribution file is stale" in capsys.readouterr().err
    assert attribution.read_text(encoding="utf-8") == "stale\n"

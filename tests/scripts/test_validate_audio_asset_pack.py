"""Independent contract tests for the public audio-pack provenance gate."""

from __future__ import annotations

import array
import hashlib
import json
import math
import re
import subprocess
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


def _generated_source(pack_dir: Path) -> tuple[dict[str, object], dict[str, bytes]]:
    revision = "1" * 40
    generator_path = "scripts/complete_audio_pack_gate.py"
    blobs = {
        generator_path: b"deterministic complete-pack generator\n",
        "scripts/core_cadence_gate.py": b"deterministic core generator\n",
        "radio.toml": b"[station]\nname = 'Mammami Radio'\n",
    }
    dependency_paths = tuple(blobs)

    def blob_url(path: str) -> str:
        return f"https://github.com/example/mammamiradio/blob/{revision}/{path}"

    master = pack_dir / "provenance" / "source-masters" / "applause.wav"
    master.parent.mkdir(parents=True, exist_ok=True)
    master_bytes = b"project-authored deterministic PCM master"
    master.write_bytes(master_bytes)
    generator: dict[str, object] = {
        "kind": validator.PROJECT_GENERATOR_KIND,
        "revision": revision,
        "path": generator_path,
        "url": blob_url(generator_path),
        "sha256": _sha256(blobs[generator_path]),
        "dependencies": [
            {"path": path, "url": blob_url(path), "sha256": _sha256(blobs[path])} for path in dependency_paths
        ],
        "runtime": {"python": "Python 3.12.11", "ffmpeg": "ffmpeg 8.0"},
    }
    return (
        _source(
            source_sha256=_sha256(master_bytes),
            source_url=blob_url(generator_path),
            original_path=master.relative_to(pack_dir).as_posix(),
            provenance_type=validator.PROJECT_AUTHORED_PROVENANCE_TYPE,
            generator=generator,
            tags=["project-authored", "electronic"],
        ),
        blobs,
    )


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


def _declared_pack_digest(payload: dict[str, object]) -> str:
    digest = hashlib.sha256()
    assets = payload["assets"]
    assert isinstance(assets, list)
    for asset in sorted(assets, key=lambda item: (str(item["path"]), str(item["id"]))):
        assert isinstance(asset, dict)
        digest.update(str(asset["path"]).encode())
        digest.update(b"\0")
        digest.update(str(asset["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _pcm_samples(count: int = 1_200, *, seed: int = 0xA341316C) -> list[int]:
    state = seed
    samples: list[int] = []
    for _ in range(count):
        state ^= (state << 13) & 0xFFFFFFFF
        state ^= state >> 17
        state ^= (state << 5) & 0xFFFFFFFF
        state &= 0xFFFFFFFF
        samples.append((state & 0xFFFF) - 32_768)
    return samples


def _correlation_grid_samples() -> int:
    return round(validator.CORRELATION_GRID_SEC * validator.CORRELATION_NATIVE_SAMPLE_RATE_HZ)


def _matching_correlation_signature() -> validator.CorrelationSignature:
    nonzero = (1 << validator.CORRELATION_MIN_COMMON_NONZERO_BITS) - 1
    return validator.CorrelationSignature(anchor=0, first_start=0, last_start=0, signs=nonzero, nonzero=nonzero)


def _landmark_index(*positions: int, key_hash: int = 1) -> validator.CorrelationLandmarkIndex:
    records = array.array(
        "Q",
        sorted((key_hash << validator.CORRELATION_LANDMARK_POSITION_BITS) | position for position in positions),
    )
    return validator.CorrelationLandmarkIndex(records)


def _sparse_landmark_index(*positions: int, distinct_keys: int = 3) -> validator.CorrelationLandmarkIndex:
    records = array.array(
        "Q",
        sorted(
            ((validator.CORRELATION_LANDMARK_SPARSE_KEY_BIT | key_hash) << validator.CORRELATION_LANDMARK_POSITION_BITS)
            | position
            for key_hash in range(1, distinct_keys + 1)
            for position in positions
        ),
    )
    return validator.CorrelationLandmarkIndex(records)


def _dense_landmark_index(position: int, *, distinct_keys: int) -> validator.CorrelationLandmarkIndex:
    records = array.array(
        "Q",
        sorted(
            (key_hash << validator.CORRELATION_LANDMARK_POSITION_BITS) | position
            for key_hash in range(1, distinct_keys + 1)
        ),
    )
    return validator.CorrelationLandmarkIndex(records)


def _sparse_landmark_index_at_distinct_positions(*positions: int) -> validator.CorrelationLandmarkIndex:
    records = array.array(
        "Q",
        sorted(
            ((validator.CORRELATION_LANDMARK_SPARSE_KEY_BIT | key_hash) << validator.CORRELATION_LANDMARK_POSITION_BITS)
            | position
            for key_hash, position in enumerate(positions, start=1)
        ),
    )
    return validator.CorrelationLandmarkIndex(records)


def _enable_quality_contract(payload: dict[str, object]) -> None:
    sources = payload["sources"]
    assets = payload["assets"]
    assert isinstance(sources, list)
    assert isinstance(assets, list)
    source_map: dict[str, dict[str, object]] = {}
    for source in sources:
        assert isinstance(source, dict)
        source.setdefault("tags", ["ambience"])
        source_map[str(source["id"])] = source
    for asset in assets:
        assert isinstance(asset, dict)
        duration = float(asset.get("duration_target_sec", 1.0))
        source_ids = asset["source_ids"]
        assert isinstance(source_ids, list)
        asset["layers"] = [
            {
                "source_id": source_id,
                "source_sha256": source_map[str(source_id)]["source_sha256"],
                "source_start_sec": 0.0,
                "duration_sec": duration,
                "output_offset_sec": 0.0,
                "gain_db": -6.0,
                "dsp": [],
                "license": source_map[str(source_id)]["license"],
                "role": "texture" if asset["kind"] == "bed" else "foreground",
            }
            for source_id in source_ids
        ]
    payload["quality_guard_allowlist"] = {"perceptual_overlaps": [], "source_core_roles": []}
    payload["pack_digest"] = _declared_pack_digest(payload)
    payload["release_ready"] = False
    payload["listening_receipt"] = {
        "status": "pending",
        "pack_digest": None,
        "approved_candidate_id": None,
        "surfaces": [],
        "device_classes": [],
        "reviewed_at": None,
    }


def _quality_review_manifest(pack_dir: Path, count: int = 2) -> dict[str, object]:
    payload = _manifest(pack_dir)
    sources = payload["sources"]
    assets = payload["assets"]
    assert isinstance(sources, list)
    assert isinstance(assets, list)
    digest_characters = "bcdef0123456789"
    for index in range(count):
        source_id = f"rhodes_{index}"
        sources.append(
            _source(
                source_id,
                source_sha256=digest_characters[index] * 64,
                title=f"Rhodes electric piano take {index}",
                tags=["rhodes"],
            )
        )
        assets.append(
            _asset(
                pack_dir,
                f"identity.review-{index}",
                f"core/review-{index}.mp3",
                [source_id],
                kind="identity",
            )
        )
    _enable_quality_contract(payload)
    return payload


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
    assert "All source recordings listed here are approved for public redistribution in this add-on." in first
    assert "All external recordings and project-authored masters listed here" not in first
    assert "## Source recordings\n" in first
    assert "## Source recordings and masters" not in first
    assert "Example performer" in first
    assert "CC0 1.0 Universal" in first
    assert "- Source: https://example.test/sounds/applause_master" in first
    assert "- Source SHA-256: `" in first
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


def test_project_authored_master_hashes_generator_dependencies_and_renders_distinct_attribution(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    generated_source, blobs = _generated_source(pack_dir)
    payload["sources"] = [generated_source]
    _write_manifest(pack_dir, payload)
    reads: list[tuple[str, str]] = []

    def read_git_blob(revision: str, path: str) -> bytes:
        reads.append((revision, path))
        return blobs[path]

    monkeypatch.setattr(validator, "_read_git_blob", read_git_blob)

    report = validator.validate_audio_asset_pack(pack_dir)
    attribution = validator.render_attribution(report)

    assert (
        "All external recordings and project-authored masters listed here are approved for public redistribution."
        in attribution
    )
    assert "## Source recordings and masters\n" in attribution
    assert sorted(path for _revision, path in reads) == sorted(blobs)
    assert "- Generator: [`scripts/complete_audio_pack_gate.py`]" in attribution
    assert "- Generator SHA-256: `" in attribution
    assert "- Generator dependency: [`scripts/core_cadence_gate.py`]" in attribution
    assert "- Generator runtime: Python `Python 3.12.11`; FFmpeg `ffmpeg 8.0`" in attribution
    assert f"- Master SHA-256: `{generated_source['source_sha256']}`" in attribution
    assert "- Source:" not in attribution
    assert "- Source SHA-256:" not in attribution


def test_project_authored_master_requires_pack_local_original(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    generated_source, blobs = _generated_source(pack_dir)
    generated_source.pop("original_path")
    payload["sources"] = [generated_source]
    _write_manifest(pack_dir, payload)
    monkeypatch.setattr(validator, "_read_git_blob", lambda _revision, path: blobs[path])

    with pytest.raises(validator.AudioAssetPackValidationError, match="original_path is required"):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("kind", "mutable-generator", "generator.kind must be"),
        ("revision", "abc", "generator.revision must be a full lowercase Git SHA"),
        ("path", "../generator.py", "generator.path must be a safe relative POSIX path"),
        ("url", "https://example.test/latest/generator.py", "generator.url must pin revision and path"),
        ("sha256", "not-a-sha", "generator.sha256 must be a lowercase 64-character SHA-256 hex digest"),
        ("dependencies", [], "generator.dependencies must be a non-empty list"),
        ("runtime", {"python": "Python 3.12.11"}, "generator.runtime.ffmpeg must be a non-empty string"),
    ],
)
def test_project_authored_master_rejects_unpinned_generator_shape(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
    message: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    generated_source, blobs = _generated_source(pack_dir)
    generator = generated_source["generator"]
    assert isinstance(generator, dict)
    generator[field] = replacement
    payload["sources"] = [generated_source]
    _write_manifest(pack_dir, payload)
    monkeypatch.setattr(validator, "_read_git_blob", lambda _revision, path: blobs[path])

    with pytest.raises(validator.AudioAssetPackValidationError, match=re.escape(message)):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("path", "../core.py", "dependencies[1].path must be a safe relative POSIX path"),
        ("url", "https://example.test/latest/core.py", "dependencies[1].url must pin revision and path"),
        ("sha256", "not-a-sha", "dependencies[1].sha256 must be a lowercase 64-character SHA-256 hex digest"),
    ],
)
def test_project_authored_master_rejects_unpinned_generator_dependency(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
    message: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    generated_source, blobs = _generated_source(pack_dir)
    generator = generated_source["generator"]
    assert isinstance(generator, dict)
    dependencies = generator["dependencies"]
    assert isinstance(dependencies, list)
    dependency = dependencies[1]
    assert isinstance(dependency, dict)
    dependency[field] = replacement
    payload["sources"] = [generated_source]
    _write_manifest(pack_dir, payload)
    monkeypatch.setattr(validator, "_read_git_blob", lambda _revision, path: blobs[path])

    with pytest.raises(validator.AudioAssetPackValidationError, match=re.escape(message)):
        validator.validate_audio_asset_pack(pack_dir)


def test_project_authored_master_source_url_must_name_the_generator(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    generated_source, blobs = _generated_source(pack_dir)
    generated_source["source_url"] = "https://example.test/master.mp3"
    payload["sources"] = [generated_source]
    _write_manifest(pack_dir, payload)
    monkeypatch.setattr(validator, "_read_git_blob", lambda _revision, path: blobs[path])

    with pytest.raises(validator.AudioAssetPackValidationError, match=r"source_url must equal.*generator.url"):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize("corrupt_path", ["scripts/complete_audio_pack_gate.py", "scripts/core_cadence_gate.py"])
def test_project_authored_master_rejects_generator_blob_hash_mismatch(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_path: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    generated_source, blobs = _generated_source(pack_dir)
    payload["sources"] = [generated_source]
    _write_manifest(pack_dir, payload)

    def read_git_blob(_revision: str, path: str) -> bytes:
        return b"different committed bytes" if path == corrupt_path else blobs[path]

    monkeypatch.setattr(validator, "_read_git_blob", read_git_blob)

    with pytest.raises(validator.AudioAssetPackValidationError, match=r"generated-source Git blob.*SHA-256 differs"):
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


def test_exact_duplicate_outputs_are_rejected_even_with_distinct_asset_ids(tmp_path: Path, fake_probe: None) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    assets = payload["assets"]
    assert isinstance(assets, list)
    first, second = assets
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    first_bytes = (pack_dir / str(first["path"])).read_bytes()
    (pack_dir / str(second["path"])).write_bytes(first_bytes)
    second["sha256"] = first["sha256"]
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError, match="exact duplicate output SHA-256"):
        validator.validate_audio_asset_pack(pack_dir)


def test_pending_receipt_is_auditionable_but_release_gate_requires_complete_approval(
    tmp_path: Path, fake_probe: None
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    _enable_quality_contract(payload)
    _write_manifest(pack_dir, payload)

    validator.validate_audio_asset_pack(pack_dir)
    with pytest.raises(validator.AudioAssetPackValidationError) as caught:
        validator.validate_audio_asset_pack(pack_dir, require_listening_approval=True)
    assert "release_ready must be true" in str(caught.value)
    assert "must be 'approved'" in str(caught.value)

    payload["release_ready"] = True
    payload["listening_receipt"] = {
        "status": "approved",
        "pack_digest": payload["pack_digest"],
        "approved_candidate_id": "midnight-signal",
        "surfaces": ["station-id", "sweeper", "ad-in", "ad-out", "full-cadence"],
        "device_classes": ["mac", "target-speaker"],
        "reviewed_at": "2026-08-13T18:30:00+02:00",
    }
    _write_manifest(pack_dir, payload)

    validator.validate_audio_asset_pack(pack_dir, require_listening_approval=True)


def test_rejected_receipt_is_digest_bound_and_never_release_ready(tmp_path: Path, fake_probe: None) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    _enable_quality_contract(payload)
    payload["listening_receipt"] = {
        "status": "rejected",
        "pack_digest": payload["pack_digest"],
        "reviewed_at": "2026-08-16T17:00:00+02:00",
        "notes": "The complete pack did not pass the listening gate.",
    }
    _write_manifest(pack_dir, payload)

    validator.validate_audio_asset_pack(pack_dir)
    with pytest.raises(validator.AudioAssetPackValidationError) as caught:
        validator.validate_audio_asset_pack(pack_dir, require_listening_approval=True)
    assert "release_ready must be true" in str(caught.value)
    assert "rejected listening receipt cannot satisfy" in str(caught.value)

    payload["release_ready"] = True
    _write_manifest(pack_dir, payload)
    with pytest.raises(validator.AudioAssetPackValidationError, match="release_ready must be false"):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("pack_digest", None, "pack_digest must match the delivered pack_digest"),
        ("reviewed_at", None, "reviewed_at is required for a rejected pack"),
        ("notes", None, "notes is required for a rejected pack"),
    ],
)
def test_rejected_receipt_requires_decision_metadata(
    tmp_path: Path,
    fake_probe: None,
    field: str,
    value: object,
    message: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    _enable_quality_contract(payload)
    payload["listening_receipt"] = {
        "status": "rejected",
        "pack_digest": payload["pack_digest"],
        "reviewed_at": "2026-08-16T17:00:00+02:00",
        "notes": "Rejected after listening.",
    }
    receipt = payload["listening_receipt"]
    assert isinstance(receipt, dict)
    receipt[field] = value
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError, match=message):
        validator.validate_audio_asset_pack(pack_dir)


def test_foreground_source_cannot_own_more_than_two_core_roles_without_reasoned_allowlist(
    tmp_path: Path, fake_probe: None
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    assets = payload["assets"]
    assert isinstance(assets, list)
    core_ids = ["identity.one", "identity.two", "transition.three"]
    for index, asset_id in enumerate(core_ids):
        assets.append(
            _asset(
                pack_dir,
                asset_id,
                f"core/{index}.mp3",
                ["applause_master"],
                kind="identity" if index < 2 else "transition",
            )
        )
    _enable_quality_contract(payload)
    guard = payload["quality_guard_allowlist"]
    assert isinstance(guard, dict)
    guard["perceptual_overlaps"] = [
        {"asset_ids": [first, second], "reason": "Fixture isolates the source-concentration invariant."}
        for first, second in ((core_ids[0], core_ids[1]), (core_ids[0], core_ids[2]), (core_ids[1], core_ids[2]))
    ]
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError, match="appears in 3 core roles"):
        validator.validate_audio_asset_pack(pack_dir)

    guard["source_core_roles"] = [
        {"source_id": "applause_master", "reason": "Three fixture roles are intentionally identical in provenance."}
    ]
    _write_manifest(pack_dir, payload)
    validator.validate_audio_asset_pack(pack_dir)


def _render_tone_mp3(
    path: Path,
    frequency_hz: float,
    *,
    duration_sec: float = 0.55,
    audio_filter: str | None = None,
    bitrate_kbps: int = 192,
) -> None:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency_hz}:sample_rate=48000:duration={duration_sec}",
    ]
    if audio_filter is not None:
        command.extend(["-af", audio_filter])
    command.extend(
        [
            "-ac",
            "2",
            "-ar",
            "48000",
            "-b:a",
            f"{bitrate_kbps}k",
            "-y",
            str(path),
        ]
    )
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _render_stereo_tone_mp3(path: Path, left_frequency_hz: float, right_frequency_hz: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={left_frequency_hz}:sample_rate=48000:duration=0.55",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={right_frequency_hz}:sample_rate=48000:duration=0.55",
            "-filter_complex",
            "[0:a][1:a]amerge=inputs=2[stereo]",
            "-map",
            "[stereo]",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-b:a",
            "192k",
            "-y",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _refresh_review_audio_ledger(pack_dir: Path, payload: dict[str, object]) -> None:
    assets = payload["assets"]
    assert isinstance(assets, list)
    for asset in assets:
        assert isinstance(asset, dict)
        if str(asset["id"]).startswith("identity.review-"):
            asset["sha256"] = _sha256((pack_dir / str(asset["path"])).read_bytes())
    payload["pack_digest"] = _declared_pack_digest(payload)
    _write_manifest(pack_dir, payload)


@pytest.mark.parametrize(
    "shift_samples",
    [
        0,
        1,
        round(0.013 * validator.CORRELATION_NATIVE_SAMPLE_RATE_HZ),
        round(0.100 * validator.CORRELATION_NATIVE_SAMPLE_RATE_HZ),
        round(0.125 * validator.CORRELATION_NATIVE_SAMPLE_RATE_HZ),
    ],
    ids=["identical", "one-sample", "thirteen-ms", "one-hundred-ms", "one-hundred-twenty-five-ms"],
)
def test_native_correlation_rejects_matching_audio_with_different_sources(
    tmp_path: Path, fake_probe: None, monkeypatch: pytest.MonkeyPatch, shift_samples: int
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    sources = payload["sources"]
    assets = payload["assets"]
    assert isinstance(sources, list)
    assert isinstance(assets, list)
    sources.extend(
        [
            _source("rhodes_one", source_sha256="b" * 64, tags=["rhodes"]),
            _source("rhodes_two", source_sha256="c" * 64, tags=["electric-piano"]),
        ]
    )
    assets.extend(
        [
            _asset(pack_dir, "identity.one", "core/one.mp3", ["rhodes_one"], kind="identity"),
            _asset(pack_dir, "identity.two", "core/two.mp3", ["rhodes_two"], kind="identity"),
        ]
    )
    _enable_quality_contract(payload)
    _write_manifest(pack_dir, payload)
    raw_samples = _pcm_samples(validator._correlation_window_samples() + 7_000)
    envelope_gains = (1, 4, 2, 7, 3, 6, 2, 5, 1, 7, 4)
    samples = [
        round(sample * envelope_gains[(index // 613) % len(envelope_gains)] / 8)
        for index, sample in enumerate(raw_samples)
    ]
    shifted_samples = ([0] * shift_samples) + samples[: len(samples) - shift_samples]
    decode_calls: list[Path] = []

    def decode(path: Path) -> list[int]:
        decode_calls.append(path)
        return shifted_samples if path.name == "two.mp3" else samples

    monkeypatch.setattr(validator, "_decode_audio", decode)

    with pytest.raises(
        validator.AudioAssetPackValidationError,
        match=r"undocumented decoded-audio correlation",
    ):
        validator.validate_audio_asset_pack(pack_dir)
    assert sorted(path.name for path in decode_calls) == ["one.mp3", "two.mp3"]

    guard = payload["quality_guard_allowlist"]
    assert isinstance(guard, dict)
    guard["perceptual_overlaps"] = [
        {
            "asset_ids": ["identity.one", "identity.two"],
            "reason": "The paired fixture intentionally verifies the documented-overlap branch.",
        }
    ]
    _write_manifest(pack_dir, payload)
    decode_calls.clear()
    validator.validate_audio_asset_pack(pack_dir)
    assert decode_calls == []


@pytest.mark.requires_ffmpeg
@pytest.mark.parametrize(
    ("first_frequency_hz", "second_frequency_hz"),
    [(600.0, 397.0), (2_510.5, 516.5), (8_000.0, 9_000.0)],
    ids=["old-997hz-alias", "two-view-absolute-alias", "within-high-band"],
)
def test_real_distinct_tones_do_not_fail_native_correlation(
    tmp_path: Path,
    fake_probe: None,
    first_frequency_hz: float,
    second_frequency_hz: float,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _render_tone_mp3(pack_dir / "core/review-0.mp3", first_frequency_hz)
    _render_tone_mp3(pack_dir / "core/review-1.mp3", second_frequency_hz, bitrate_kbps=160)
    _refresh_review_audio_ledger(pack_dir, payload)

    validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.requires_ffmpeg
@pytest.mark.parametrize("frequency_hz", [7_000.0, 12_000.0], ids=["seven-khz", "twelve-khz"])
def test_real_reencoded_shifted_high_frequency_tone_is_rejected(
    tmp_path: Path,
    fake_probe: None,
    frequency_hz: float,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _render_tone_mp3(pack_dir / "core/review-0.mp3", frequency_hz)
    _render_tone_mp3(
        pack_dir / "core/review-1.mp3",
        frequency_hz,
        audio_filter="volume=-0.55,adelay=37S:all=1",
        bitrate_kbps=160,
    )
    _refresh_review_audio_ledger(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.requires_ffmpeg
def test_real_common_left_channel_is_not_hidden_by_different_right_channels(
    tmp_path: Path,
    fake_probe: None,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _render_stereo_tone_mp3(pack_dir / "core/review-0.mp3", 7_000.0, 900.0)
    _render_stereo_tone_mp3(pack_dir / "core/review-1.mp3", 7_000.0, 3_100.0)
    _refresh_review_audio_ledger(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError, match=r"\(L-L, lag"):
        validator.validate_audio_asset_pack(pack_dir)


def test_antiphase_stereo_cancellation_cannot_hide_a_common_channel(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    common = [round(sample / 2) for sample in _pcm_samples(validator._correlation_window_samples())]
    first = validator.DecodedAudio(
        left=array.array("h", common),
        right=array.array("h", (-sample for sample in common)),
    )
    second = validator.DecodedAudio(
        left=array.array("h", (round(0.7 * sample) for sample in common)),
        right=array.array("h", _pcm_samples(validator._correlation_window_samples(), seed=0xDEADBEEF)),
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second if path.name == "review-1.mp3" else first,
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match=r"\(L-L, lag"):
        validator.validate_audio_asset_pack(pack_dir)


def test_common_audio_moving_from_left_to_right_channel_is_rejected(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    common = [round(sample / 4) for sample in _pcm_samples(window_samples, seed=0x12345678)]
    transformed_common = [500 - 2 * sample for sample in common]
    assert (
        validator._maximum_native_correlation_in_region(common, transformed_common, 0, 0, 0)
        >= validator.CORRELATION_THRESHOLD
    )
    first = validator.DecodedAudio(
        left=array.array("h", common),
        right=array.array("h", _pcm_samples(window_samples, seed=0x11111111)),
    )
    second = validator.DecodedAudio(
        left=array.array("h", _pcm_samples(window_samples, seed=0x22222222)),
        right=array.array("h", transformed_common),
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second if path.name == "review-1.mp3" else first,
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match=r"\(L-R, lag"):
        validator.validate_audio_asset_pack(pack_dir)


def test_native_search_confirms_an_isolated_off_hop_excerpt_with_gain_dc_and_polarity(
    tmp_path: Path, fake_probe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    common = _pcm_samples(window_samples, seed=0x12345678)
    first_start = 125
    second_start = 77
    first_samples = _pcm_samples(first_start, seed=0x11111111) + common + _pcm_samples(125, seed=0x22222222)
    transformed_common = [round(-0.7 * sample) + 1_000 for sample in common]
    second_samples = (
        _pcm_samples(second_start, seed=0x33333333) + transformed_common + _pcm_samples(173, seed=0x44444444)
    )
    expected_lag = second_start - first_start
    assert (
        validator._maximum_native_correlation_at_lag(first_samples, second_samples, expected_lag)
        >= validator.CORRELATION_THRESHOLD
    )

    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )
    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_decoy_signature_match_does_not_suppress_independent_landmark_recovery(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    common = [0] * window_samples
    transient_start = window_samples // 2
    common[transient_start : transient_start + 4] = [12_000, -12_000, 8_000, -8_000]
    transformed_common = [1_000 - 2 * sample for sample in common]
    first_start = 125
    second_start = 77
    first_samples = _pcm_samples(125, seed=0x11111111) + common + _pcm_samples(125, seed=0x22222222)
    second_samples = _pcm_samples(77, seed=0x33333333) + transformed_common + _pcm_samples(173, seed=0x44444444)
    assert len(first_samples) == len(second_samples)
    assert (
        validator._maximum_native_correlation_in_region(first_samples, second_samples, 0, 0, 0)
        < validator.CORRELATION_THRESHOLD
    )
    expected_lag = second_start - first_start
    assert (
        validator._maximum_native_correlation_in_region(
            first_samples,
            second_samples,
            expected_lag,
            first_start,
            first_start,
        )
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )
    decoy_signature = _matching_correlation_signature()
    monkeypatch.setattr(
        validator,
        "_build_pair_projection_signatures",
        lambda _samples: (decoy_signature,),
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize("edge", ["start", "end"])
def test_sparse_affine_transient_at_exact_window_boundary_is_rejected(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
    edge: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    transient_start = 0 if edge == "start" else window_samples - 4
    first_samples = [0] * window_samples
    first_samples[transient_start : transient_start + 4] = [12_000, -12_000, 8_000, -8_000]
    second_samples = [1_000 - 2 * sample for sample in first_samples]
    first_landmarks, first_event_count = validator._build_landmark_postings(first_samples)
    second_landmarks, second_event_count = validator._build_landmark_postings(second_samples)
    assert first_event_count == len(first_landmarks.records) > 0
    assert second_event_count == len(second_landmarks.records) > 0
    landmark_regions, landmark_lag_count = validator._matched_landmark_regions(
        first_landmarks,
        second_landmarks,
        len(first_samples),
        len(second_samples),
        maximum_segments=validator.CORRELATION_MAX_LANDMARK_REGION_PROBES,
    )
    assert (0, 0, 0) in landmark_regions
    assert landmark_lag_count > 0
    assert (
        validator._maximum_native_correlation_in_region(first_samples, second_samples, 0, 0, 0)
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )
    monkeypatch.setattr(validator, "_build_pair_projection_signatures", lambda _samples: ())

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_retained_band_screen_rejects_a_sparse_shared_off_hop_transient(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    sample_count = validator._correlation_window_samples() + 2_000
    first_samples = [0] * sample_count
    first_samples[700:704] = [12_000, -8_000, 5_000, -3_000]
    shift_samples = 37
    second_samples = [0] * sample_count
    second_samples[700 + shift_samples : 704 + shift_samples] = [-24_000, 16_000, -10_000, 6_000]

    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )
    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize(
    "transient_phase_samples",
    [0, _correlation_grid_samples() // 2],
    ids=["grid", "half-grid"],
)
def test_half_grid_low_level_affine_transient_survives_louder_disjoint_context(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
    transient_phase_samples: int,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    half_grid = _correlation_grid_samples() // 2
    transient_start = window_samples // 2 + transient_phase_samples
    first_common = [0] * window_samples
    first_common[transient_start : transient_start + 4] = [12_000, -12_000, 8_000, -8_000]
    second_common = [1_000 - 2 * sample for sample in first_common]
    first_outer = [30_000, -30_000] * (half_grid // 2)
    second_outer = [30_000] * (half_grid // 4) + [-30_000] * (half_grid // 2) + [30_000] * (half_grid // 4)
    first_samples = first_outer + first_common + first_outer
    second_samples = second_outer + second_common + second_outer
    assert len(first_samples) == len(second_samples) == window_samples + _correlation_grid_samples()
    assert (
        validator._maximum_native_correlation_in_region(
            first_samples,
            second_samples,
            0,
            half_grid,
            half_grid,
        )
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize("common_edge", ["start", "end"])
def test_candidate_window_boundary_transient_survives_louder_disjoint_context(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
    common_edge: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    half_grid = _correlation_grid_samples() // 2
    transient_start = 0 if common_edge == "start" else window_samples - 4
    first_common = [0] * window_samples
    first_common[transient_start : transient_start + 4] = [12_000, -12_000, 8_000, -8_000]
    second_common = [1_000 - 2 * sample for sample in first_common]
    first_outer = [30_000, -30_000] * (half_grid // 2)
    second_outer = [30_000] * (half_grid // 4) + [-30_000] * (half_grid // 2) + [30_000] * (half_grid // 4)
    first_samples = first_outer + first_common + first_outer
    second_samples = second_outer + second_common + second_outer
    assert (
        validator._maximum_native_correlation_in_region(
            first_samples,
            second_samples,
            0,
            half_grid,
            half_grid,
        )
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_clustered_two_key_sparse_excerpt_is_rejected_inside_long_disjoint_context(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    context_samples = 4_000
    first_common = [0] * window_samples
    first_common[-4:] = [12_000, -12_000, 8_000, -8_000]
    second_common = [1_000 - 2 * sample for sample in first_common]
    first_samples = (
        _pcm_samples(context_samples, seed=0x11111111) + first_common + _pcm_samples(context_samples, seed=0x22222222)
    )
    second_samples = (
        _pcm_samples(context_samples, seed=0x33333333) + second_common + _pcm_samples(context_samples, seed=0x44444444)
    )
    first_landmarks, _first_event_count = validator._build_landmark_postings(first_samples)
    second_landmarks, _second_event_count = validator._build_landmark_postings(second_samples)
    first_positions_by_key: dict[int, set[int]] = {}
    second_positions_by_key: dict[int, set[int]] = {}
    for record in first_landmarks.records:
        key = record >> validator.CORRELATION_LANDMARK_POSITION_BITS
        position = record & validator.CORRELATION_LANDMARK_POSITION_MASK
        first_positions_by_key.setdefault(key, set()).add(position)
    for record in second_landmarks.records:
        key = record >> validator.CORRELATION_LANDMARK_POSITION_BITS
        position = record & validator.CORRELATION_LANDMARK_POSITION_MASK
        second_positions_by_key.setdefault(key, set()).add(position)
    shared_sparse_evidence = [
        (key, position)
        for key in first_positions_by_key.keys() & second_positions_by_key.keys()
        if key & validator.CORRELATION_LANDMARK_SPARSE_KEY_BIT
        for position in first_positions_by_key[key] & second_positions_by_key[key]
    ]
    shared_sparse_keys = {key for key, _position in shared_sparse_evidence}
    shared_sparse_positions = [position for _key, position in shared_sparse_evidence]
    assert len(shared_sparse_keys) == 2
    assert max(shared_sparse_positions) - min(shared_sparse_positions) <= 8
    assert (
        validator._maximum_native_correlation_in_region(
            first_samples,
            second_samples,
            0,
            context_samples,
            context_samples,
        )
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )
    monkeypatch.setattr(validator, "_build_pair_projection_signatures", lambda _samples: ())

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_half_fine_grid_low_level_dense_excerpt_survives_louder_disjoint_context(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    half_fine_grid = round(0.001 * validator.CORRELATION_NATIVE_SAMPLE_RATE_HZ)
    common = [round(sample / 32) for sample in _pcm_samples(window_samples, seed=0xD3A5E123)]
    transformed_common = [500 - 4 * sample for sample in common]
    first_outer = [30_000, -30_000] * (half_fine_grid // 2)
    second_outer = (
        [30_000] * (half_fine_grid // 4) + [-30_000] * (half_fine_grid // 2) + [30_000] * (half_fine_grid // 4)
    )
    first_samples = first_outer + common + first_outer
    second_samples = second_outer + transformed_common + second_outer
    assert (
        validator._maximum_native_correlation_in_region(
            first_samples,
            second_samples,
            0,
            half_fine_grid,
            half_fine_grid,
        )
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_half_fine_grid_zero_mean_triangle_survives_louder_disjoint_context(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    half_fine_grid = round(0.001 * validator.CORRELATION_NATIVE_SAMPLE_RATE_HZ)
    common = [
        -480 + 10 * phase if phase < 96 else 480 - 10 * (phase - 96)
        for index in range(window_samples)
        for phase in (index % 192,)
    ]
    assert sum(common) == 0
    assert {abs(common[index] - common[index - 1]) for index in range(1, len(common))} == {10}
    transformed_common = [1_000 - 4 * sample for sample in common]
    first_outer = [30_000, -30_000] * (half_fine_grid // 2)
    second_outer = (
        [30_000] * (half_fine_grid // 4) + [-30_000] * (half_fine_grid // 2) + [30_000] * (half_fine_grid // 4)
    )
    first_samples = first_outer + common + first_outer
    second_samples = second_outer + transformed_common + second_outer
    assert (
        validator._maximum_native_correlation_in_region(
            first_samples,
            second_samples,
            0,
            half_fine_grid,
            half_fine_grid,
        )
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_shared_transient_with_distinct_rest_does_not_false_positive(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    first_samples = [round(sample / 8) for sample in _pcm_samples(window_samples, seed=0x01020304)]
    second_samples = [round(sample / 8) for sample in _pcm_samples(window_samples, seed=0xA0B0C0D0)]
    transient_start = window_samples // 2 + _correlation_grid_samples() // 2
    shared_transient = [12_000, -12_000, 8_000, -8_000]
    first_samples[transient_start : transient_start + len(shared_transient)] = shared_transient
    second_samples[transient_start : transient_start + len(shared_transient)] = shared_transient
    assert (
        validator._maximum_native_correlation_in_region(first_samples, second_samples, 0, 0, 0)
        < validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )

    validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize(
    ("first_remainder", "second_remainder"),
    [(137, _correlation_grid_samples() - 1), (_correlation_grid_samples() - 1, 137)],
    ids=["short-first", "short-second"],
)
def test_low_level_dense_common_window_is_found_at_unframed_clip_remainder(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
    first_remainder: int,
    second_remainder: int,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    common = [round(sample / 16) for sample in _pcm_samples(window_samples, seed=0xA55A5AA5)]
    transformed_common = [500 - 2 * sample for sample in common]
    first_samples = _pcm_samples(window_samples + first_remainder, seed=0x11111111)
    second_samples = _pcm_samples(window_samples + second_remainder, seed=0x22222222)
    first_samples[first_remainder:] = common
    second_samples[second_remainder:] = transformed_common
    expected_lag = second_remainder - first_remainder
    assert (
        validator._maximum_native_correlation_in_region(
            first_samples,
            second_samples,
            expected_lag,
            first_remainder,
            first_remainder,
        )
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_native_search_rejects_dense_correlation_just_above_threshold(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    first_samples = _pcm_samples(validator._correlation_window_samples(), seed=0x12345678)
    noise = _pcm_samples(validator._correlation_window_samples(), seed=0x87654321)
    second_samples = [
        max(-32_768, min(32_767, round(0.7 * sample + 0.135 * perturbation)))
        for sample, perturbation in zip(first_samples, noise, strict=True)
    ]
    exact_correlation = validator._maximum_native_correlation_at_lag(first_samples, second_samples, 0)
    assert validator.CORRELATION_THRESHOLD <= exact_correlation < validator.CORRELATION_THRESHOLD + 0.005

    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )
    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_exact_500ms_gain_and_polarity_duplicate_reaches_native_confirmation(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    first_samples = [round(sample * 0.2) for sample in _pcm_samples(validator._correlation_window_samples())]
    second_samples = [round(-1.5 * sample) + 1_000 for sample in first_samples]
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_screen_is_invariant_to_large_dc_offsets_on_low_amplitude_common_audio(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    common = [round(sample / 256) for sample in _pcm_samples(validator._correlation_window_samples())]
    prefix_samples = 5_000
    suffix_samples = 4_000
    first_samples = (
        _pcm_samples(prefix_samples, seed=0x11111111) + common + _pcm_samples(suffix_samples, seed=0x22222222)
    )
    second_samples = [0] * prefix_samples + [20_000 - 2 * sample for sample in common] + [0] * suffix_samples
    assert (
        validator._maximum_native_correlation_at_lag(first_samples, second_samples, 0)
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_screen_is_context_independent_for_affine_dynamic_envelope_excerpt(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    base = [round(sample / 32) for sample in _pcm_samples(window_samples, seed=0xC0FFEE)]
    common = [sample if index < window_samples // 2 else 10 * sample for index, sample in enumerate(base)]
    prefix_samples = 4_000
    suffix_samples = 3_000
    first_samples = (
        [round(sample / 2) for sample in _pcm_samples(prefix_samples, seed=0x11111111)]
        + common
        + [round(sample / 2) for sample in _pcm_samples(suffix_samples, seed=0x22222222)]
    )
    second_samples = (
        [round(sample / 512) for sample in _pcm_samples(prefix_samples, seed=0x33333333)]
        + [5_000 + 2 * sample for sample in common]
        + [round(sample / 512) for sample in _pcm_samples(suffix_samples, seed=0x44444444)]
    )
    assert (
        validator._maximum_native_correlation_at_lag(first_samples, second_samples, 0)
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_screen_keeps_sparse_loud_common_audio_over_disjoint_nonzero_tails(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    common_samples = round(0.050 * validator.CORRELATION_NATIVE_SAMPLE_RATE_HZ)
    common = _pcm_samples(common_samples, seed=0x5A45E)
    tail_samples = window_samples - common_samples
    first_tail = [24 if (index // 120) % 2 else -24 for index in range(tail_samples)]
    second_tail = [24 if index % 2 else -24 for index in range(tail_samples)]
    first_samples = common + first_tail
    second_samples = [round(-0.7 * sample) + 500 for sample in common] + second_tail
    assert (
        validator._maximum_native_correlation_at_lag(first_samples, second_samples, 0)
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )
    monkeypatch.setattr(
        validator,
        "_build_landmark_postings",
        lambda _samples, **_kwargs: (_landmark_index(), 0),
    )
    assert validator.CORRELATION_SIGNATURE_DISTANCE == 40

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_slow_shared_plateau_survives_frame_local_demeaning_screen(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    sample_rate = validator.CORRELATION_NATIVE_SAMPLE_RATE_HZ
    window_samples = validator._correlation_window_samples()
    first_samples: list[int] = []
    second_samples: list[int] = []
    for index in range(window_samples):
        plateau = 10_000 if index < window_samples // 2 else -10_000
        first_residual = round(1_000 * math.sin(2 * math.pi * 200 * index / sample_rate))
        second_residual = round(1_000 * math.sin(2 * math.pi * 10_000 * index / sample_rate))
        first_samples.append(plateau + first_residual)
        second_samples.append(round(-0.8 * plateau) + 5_000 + second_residual)
    assert (
        validator._maximum_native_correlation_at_lag(first_samples, second_samples, 0)
        >= validator.CORRELATION_THRESHOLD
    )
    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )
    monkeypatch.setattr(
        validator,
        "_build_landmark_postings",
        lambda _samples, **_kwargs: (_landmark_index(), 0),
    )
    assert validator.CORRELATION_SIGNATURE_DISTANCE == 40

    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_native_search_rejects_adversarial_perturbed_probe_coordinates(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    window_samples = validator._correlation_window_samples()
    first_samples = _pcm_samples(window_samples, seed=0x5EED1234)
    second_samples = list(first_samples)
    probe_coordinates = (0, 29, 67, 113, 167, 223, 281, 347)
    first_pattern = (0, 1, 2, 3, 4, 5, 6, 7)
    perturbed_pattern = (3, 0, 7, 1, 6, 2, 5, 4)
    for coordinate, first_value, second_value in zip(
        probe_coordinates,
        first_pattern,
        perturbed_pattern,
        strict=True,
    ):
        first_samples[coordinate] = first_value
        second_samples[coordinate] = second_value
    assert validator._maximum_native_correlation_at_lag(first_samples, second_samples, 0) > 0.999_999

    monkeypatch.setattr(
        validator,
        "_decode_audio",
        lambda path: second_samples if path.name == "review-1.mp3" else first_samples,
    )
    with pytest.raises(validator.AudioAssetPackValidationError, match="undocumented decoded-audio correlation"):
        validator.validate_audio_asset_pack(pack_dir)


def test_reversed_allowlist_pair_does_not_exempt_a_third_matching_asset(
    tmp_path: Path, fake_probe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir, count=3)
    guard = payload["quality_guard_allowlist"]
    assert isinstance(guard, dict)
    guard["perceptual_overlaps"] = [
        {
            "asset_ids": ["identity.review-1", "identity.review-0"],
            "reason": "The reversed fixture pair is intentionally documented.",
        }
    ]
    _write_manifest(pack_dir, payload)
    decode_calls: list[str] = []

    def decode(path: Path) -> list[int]:
        decode_calls.append(path.name)
        return _pcm_samples(validator._correlation_window_samples() + 500)

    monkeypatch.setattr(validator, "_decode_audio", decode)

    with pytest.raises(validator.AudioAssetPackValidationError) as caught:
        validator.validate_audio_asset_pack(pack_dir)
    message = str(caught.value)
    assert "identity.review-0' and 'identity.review-2" in message
    assert sorted(decode_calls) == ["review-0.mp3", "review-1.mp3", "review-2.mp3"]


def test_perceptual_overlap_reviewed_asset_budget_fails_before_decode(
    tmp_path: Path, fake_probe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    monkeypatch.setattr(validator, "CORRELATION_MAX_REVIEW_ASSETS", 1)
    decode_calls: list[Path] = []

    def decode(path: Path) -> list[int]:
        decode_calls.append(path)
        return _pcm_samples()

    monkeypatch.setattr(validator, "_decode_audio", decode)

    with pytest.raises(validator.AudioAssetPackValidationError, match="reviewed-asset budget exceeded"):
        validator.validate_audio_asset_pack(pack_dir)
    assert decode_calls == []


def test_perceptual_overlap_decoded_sample_budget_fails_closed(
    tmp_path: Path, fake_probe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    monkeypatch.setattr(validator, "CORRELATION_MAX_SAMPLES_PER_ASSET", 100)
    decode_calls: list[Path] = []

    def decode(path: Path) -> list[int]:
        decode_calls.append(path)
        return _pcm_samples(101)

    monkeypatch.setattr(validator, "_decode_audio", decode)

    with pytest.raises(validator.AudioAssetPackValidationError, match="decoded-sample budget"):
        validator.validate_audio_asset_pack(pack_dir)
    assert len(decode_calls) == 1


def test_perceptual_overlap_total_decoded_sample_budget_fails_closed(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    monkeypatch.setattr(validator, "CORRELATION_MAX_SAMPLES_PER_ASSET", 200)
    monkeypatch.setattr(validator, "CORRELATION_MAX_TOTAL_SAMPLES", 150)
    decode_calls: list[Path] = []

    def decode(path: Path) -> list[int]:
        decode_calls.append(path)
        return _pcm_samples(100)

    monkeypatch.setattr(validator, "_decode_audio", decode)

    with pytest.raises(validator.AudioAssetPackValidationError, match="total decoded-sample budget exceeded"):
        validator.validate_audio_asset_pack(pack_dir)
    assert len(decode_calls) == 2


def test_ffmpeg_decode_is_bounded_before_stdout_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validator, "CORRELATION_MAX_SAMPLES_PER_ASSET", 10)
    captured_command: list[str] = []
    captured_options: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured_command.extend(command)
        captured_options.update(kwargs)
        return subprocess.CompletedProcess(command, 0, b"\0\0" * 22, b"")

    monkeypatch.setattr(validator.subprocess, "run", run)

    samples = validator._decode_audio(tmp_path / "bounded.mp3")

    duration_index = captured_command.index("-t") + 1
    assert float(captured_command[duration_index]) == pytest.approx(
        11 / validator.CORRELATION_NATIVE_SAMPLE_RATE_HZ,
        abs=1e-9,
    )
    byte_limit_index = captured_command.index("-fs") + 1
    assert captured_command[byte_limit_index] == "44"
    assert captured_options["stdout"] is subprocess.PIPE
    assert captured_options["stderr"] is subprocess.DEVNULL
    assert "capture_output" not in captured_options
    assert captured_command[captured_command.index("-ar") + 1] == str(validator.CORRELATION_NATIVE_SAMPLE_RATE_HZ)
    assert "aresample" not in " ".join(captured_command)
    assert samples.sample_count == 11
    assert samples.left == samples.right


@pytest.mark.parametrize(
    ("budget_name", "message"),
    [
        ("CORRELATION_MAX_SIGNATURE_COORDINATES", "signature-coordinate budget exceeded"),
        ("CORRELATION_MAX_SIGNATURE_WORK", "signature-work budget exceeded"),
    ],
)
def test_signature_budgets_fail_before_signature_materialization(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    message: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    monkeypatch.setattr(validator, budget_name, 0)
    samples = _pcm_samples(validator._correlation_window_samples())
    monkeypatch.setattr(validator, "_decode_audio", lambda _path: samples)

    def unexpected_signatures(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("signatures were materialized before the global signature budget passed")

    monkeypatch.setattr(validator, "_build_pair_projection_signatures", unexpected_signatures)

    with pytest.raises(validator.AudioAssetPackValidationError, match=message):
        validator.validate_audio_asset_pack(pack_dir)


def test_signature_match_budget_stops_streaming_before_native_region_materialization(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    samples = _pcm_samples(validator._correlation_window_samples())
    signature = _matching_correlation_signature()
    monkeypatch.setattr(validator, "CORRELATION_MAX_SIGNATURE_MATCHES", 1)
    monkeypatch.setattr(validator, "_decode_audio", lambda _path: samples)
    monkeypatch.setattr(validator, "_build_pair_projection_signatures", lambda _samples: (signature,))
    monkeypatch.setattr(
        validator,
        "_build_landmark_postings",
        lambda _samples, **_kwargs: (_landmark_index(), 0),
    )

    def matches(*_args: object, **_kwargs: object) -> object:
        yield (0, 0, 0, 0)
        yield (0, 0, 0, 0)
        raise AssertionError("signature matching continued after the match budget was exceeded")

    def unexpected_regions(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("native regions were materialized after the signature-match budget failed")

    monkeypatch.setattr(validator, "_iter_pair_projection_matches", matches)
    monkeypatch.setattr(validator, "_add_rectangle_regions", unexpected_regions)

    with pytest.raises(validator.AudioAssetPackValidationError, match="signature-match budget exceeded"):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize(
    ("budget_name", "message"),
    [
        ("CORRELATION_MAX_LANDMARK_EVENTS", "landmark-event/posting budget exceeded"),
        ("CORRELATION_MAX_LANDMARK_POSTINGS", "landmark-event/posting budget exceeded"),
    ],
)
def test_landmark_index_budgets_fail_before_channel_pair_validation(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    message: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    samples = _pcm_samples(validator._correlation_window_samples())
    monkeypatch.setattr(validator, budget_name, 0)
    monkeypatch.setattr(validator, "_decode_audio", lambda _path: samples)
    monkeypatch.setattr(validator, "_iter_pair_projection_matches", lambda *_args: iter(()))

    def overflow_landmarks(_samples: object, *, maximum_events: int) -> object:
        assert maximum_events == 0
        raise OverflowError("fixture landmark budget")

    monkeypatch.setattr(validator, "_build_landmark_postings", overflow_landmarks)

    original_plan = validator._plan_correlation_channel_pair

    def guarded_plan(*args: object, **kwargs: object) -> object:
        if kwargs.get("use_landmarks") is True:
            raise AssertionError("landmark pair validation ran after the landmark index budget failed")
        return original_plan(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(validator, "_plan_correlation_channel_pair", guarded_plan)

    with pytest.raises(validator.AudioAssetPackValidationError, match=message):
        validator.validate_audio_asset_pack(pack_dir)


def test_landmark_join_budget_fails_before_lag_materialization(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    samples = _pcm_samples(validator._correlation_window_samples())
    signature = _matching_correlation_signature()
    landmark = _landmark_index(100)
    monkeypatch.setattr(validator, "CORRELATION_MAX_LANDMARK_JOINS", 0)
    monkeypatch.setattr(validator, "_decode_audio", lambda _path: samples)
    monkeypatch.setattr(validator, "_build_pair_projection_signatures", lambda _samples: (signature,))
    monkeypatch.setattr(validator, "_build_landmark_postings", lambda _samples, **_kwargs: (landmark, 1))
    monkeypatch.setattr(validator, "_iter_pair_projection_matches", lambda *_args: iter(()))

    def unexpected_lags(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("landmark lags were materialized after the join budget failed")

    monkeypatch.setattr(validator, "_matched_landmark_regions", unexpected_lags)

    with pytest.raises(validator.AudioAssetPackValidationError, match="landmark-join budget exceeded"):
        validator.validate_audio_asset_pack(pack_dir)


def test_landmark_lag_budget_fails_before_native_region_materialization(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    samples = _pcm_samples(validator._correlation_window_samples())
    signature = _matching_correlation_signature()
    landmark = _sparse_landmark_index(100)
    monkeypatch.setattr(validator, "CORRELATION_MAX_LANDMARK_LAGS", 0)
    monkeypatch.setattr(validator, "_decode_audio", lambda _path: samples)
    monkeypatch.setattr(validator, "_build_pair_projection_signatures", lambda _samples: (signature,))
    monkeypatch.setattr(validator, "_build_landmark_postings", lambda _samples, **_kwargs: (landmark, 1))
    monkeypatch.setattr(validator, "_iter_pair_projection_matches", lambda *_args: iter(()))

    original_merge = validator._merge_native_regions
    merge_calls = 0

    def guarded_merge(
        intervals_by_lag: dict[int, list[tuple[int, int]]],
        full_lags: set[int],
        first_sample_count: int,
        second_sample_count: int,
    ) -> object:
        nonlocal merge_calls
        merge_calls += 1
        if merge_calls > 1:
            raise AssertionError("native regions were materialized after the landmark-lag budget failed")
        return original_merge(intervals_by_lag, full_lags, first_sample_count, second_sample_count)

    monkeypatch.setattr(validator, "_merge_native_regions", guarded_merge)

    with pytest.raises(validator.AudioAssetPackValidationError, match="landmark-lag budget exceeded"):
        validator.validate_audio_asset_pack(pack_dir)


def test_landmark_segment_budget_fails_before_second_exact_interval_append() -> None:
    window_samples = validator._correlation_window_samples()
    landmark = _sparse_landmark_index(window_samples // 2, 2 * window_samples)

    with pytest.raises(OverflowError, match="landmark-region budget exceeded"):
        validator._matched_landmark_regions(
            landmark,
            landmark,
            3 * window_samples,
            3 * window_samples,
            maximum_segments=1,
        )


def test_sparse_landmark_center_rejects_one_descriptor_key() -> None:
    window_samples = validator._correlation_window_samples()
    event_position = window_samples // 2
    landmark = _sparse_landmark_index(event_position, distinct_keys=1)

    regions, lag_count = validator._matched_landmark_regions(
        landmark,
        landmark,
        window_samples,
        window_samples,
        maximum_segments=validator.CORRELATION_MAX_LANDMARK_REGION_PROBES,
    )

    assert regions == ()
    assert lag_count == 0


@pytest.mark.parametrize(
    ("position_delta", "expected_nomination"),
    [(8, True), (9, False)],
    ids=["clustered", "unclustered"],
)
def test_two_key_sparse_landmark_center_requires_eight_sample_cluster(
    position_delta: int,
    expected_nomination: bool,
) -> None:
    window_samples = validator._correlation_window_samples()
    first_position = window_samples // 2
    landmark = _sparse_landmark_index_at_distinct_positions(first_position, first_position + position_delta)

    regions, lag_count = validator._matched_landmark_regions(
        landmark,
        landmark,
        window_samples,
        window_samples,
        maximum_segments=validator.CORRELATION_MAX_LANDMARK_REGION_PROBES,
    )

    assert bool(regions) is expected_nomination
    assert bool(lag_count) is expected_nomination


def test_three_key_sparse_landmark_center_does_not_require_clustering() -> None:
    window_samples = validator._correlation_window_samples()
    landmark = _sparse_landmark_index_at_distinct_positions(
        window_samples // 4,
        window_samples // 2,
        3 * window_samples // 4,
    )

    regions, lag_count = validator._matched_landmark_regions(
        landmark,
        landmark,
        window_samples,
        window_samples,
        maximum_segments=validator.CORRELATION_MAX_LANDMARK_REGION_PROBES,
    )

    assert regions == ((0, 0, 0),)
    assert lag_count == 1


@pytest.mark.parametrize(("distinct_keys", "expected_nomination"), [(39, False), (40, True)])
def test_dense_landmark_center_requires_forty_distinct_descriptor_keys(
    distinct_keys: int,
    expected_nomination: bool,
) -> None:
    window_samples = validator._correlation_window_samples()
    event_position = window_samples // 2
    landmark = _dense_landmark_index(event_position, distinct_keys=distinct_keys)

    regions, lag_count = validator._matched_landmark_regions(
        landmark,
        landmark,
        window_samples,
        window_samples,
        maximum_segments=validator.CORRELATION_MAX_LANDMARK_REGION_PROBES,
    )

    assert bool(regions) is expected_nomination
    assert bool(lag_count) is expected_nomination


@pytest.mark.parametrize(
    ("budget_name", "message"),
    [
        ("CORRELATION_MAX_NATIVE_LAG_CANDIDATES", "native-lag budget exceeded"),
        ("CORRELATION_MAX_NATIVE_EXACT_WINDOWS", "native-window budget exceeded"),
        ("CORRELATION_MAX_NATIVE_PRIMITIVE_WORK", "native-work budget exceeded"),
    ],
)
def test_perceptual_overlap_native_budgets_fail_before_confirmation(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    message: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    samples = _pcm_samples(validator._correlation_window_samples())
    signature = _matching_correlation_signature()
    monkeypatch.setattr(validator, budget_name, 0)
    monkeypatch.setattr(validator, "_decode_audio", lambda _path: samples)
    monkeypatch.setattr(validator, "_build_pair_projection_signatures", lambda _samples: (signature,))
    monkeypatch.setattr(
        validator,
        "_build_landmark_postings",
        lambda _samples, **_kwargs: (_landmark_index(), 0),
    )

    def unexpected_confirmation(*_args: object, **_kwargs: object) -> float:
        raise AssertionError("native confirmation ran before cumulative workload was accepted")

    monkeypatch.setattr(validator, "_maximum_native_correlation_in_region", unexpected_confirmation)

    with pytest.raises(validator.AudioAssetPackValidationError, match=message):
        validator.validate_audio_asset_pack(pack_dir)


def test_signature_region_budget_fails_before_native_region_merge(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    samples = _pcm_samples(validator._correlation_window_samples())
    signature = _matching_correlation_signature()
    monkeypatch.setattr(validator, "CORRELATION_MAX_NATIVE_REGIONS", 0)
    monkeypatch.setattr(validator, "_decode_audio", lambda _path: samples)
    monkeypatch.setattr(validator, "_build_pair_projection_signatures", lambda _samples: (signature,))
    monkeypatch.setattr(
        validator,
        "_build_landmark_postings",
        lambda _samples, **_kwargs: (_landmark_index(), 0),
    )

    def unexpected_merge(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("native regions were merged after the signature-region budget failed")

    monkeypatch.setattr(validator, "_merge_native_regions", unexpected_merge)

    with pytest.raises(validator.AudioAssetPackValidationError, match="native-region preallocation budget exceeded"):
        validator.validate_audio_asset_pack(pack_dir)


def test_landmark_region_budget_fails_before_native_region_merge(
    tmp_path: Path,
    fake_probe: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _quality_review_manifest(pack_dir)
    _write_manifest(pack_dir, payload)
    samples = _pcm_samples(validator._correlation_window_samples())
    signature = _matching_correlation_signature()
    monkeypatch.setattr(validator, "CORRELATION_MAX_NATIVE_REGIONS", 0)
    monkeypatch.setattr(validator, "_decode_audio", lambda _path: samples)
    monkeypatch.setattr(validator, "_build_pair_projection_signatures", lambda _samples: (signature,))
    monkeypatch.setattr(
        validator,
        "_build_landmark_postings",
        lambda _samples, **_kwargs: (_landmark_index(), 0),
    )
    monkeypatch.setattr(validator, "_iter_pair_projection_matches", lambda *_args: iter(()))
    monkeypatch.setattr(validator, "_matched_landmark_regions", lambda *_args, **_kwargs: (((0, 0, 0),), 1))

    original_merge = validator._merge_native_regions
    merge_calls = 0

    def guarded_merge(
        intervals_by_lag: dict[int, list[tuple[int, int]]],
        full_lags: set[int],
        first_sample_count: int,
        second_sample_count: int,
    ) -> object:
        nonlocal merge_calls
        merge_calls += 1
        if merge_calls > 1:
            raise AssertionError("native regions were merged after the landmark-region budget failed")
        return original_merge(intervals_by_lag, full_lags, first_sample_count, second_sample_count)

    monkeypatch.setattr(validator, "_merge_native_regions", guarded_merge)

    with pytest.raises(validator.AudioAssetPackValidationError, match="native-region preallocation budget exceeded"):
        validator.validate_audio_asset_pack(pack_dir)


def test_signature_cells_cover_every_native_start_exactly_once() -> None:
    cells = validator._signature_start_cells(479)
    coordinates = [
        coordinate for _anchor, first_start, last_start in cells for coordinate in range(first_start, last_start + 1)
    ]

    assert coordinates == list(range(480))
    assert len(coordinates) == len(set(coordinates))
    assert cells[0][0] == 0
    assert cells[-1][0] == 479


def test_pair_projection_requires_32_common_nonzero_bits_and_accepts_polarity_inversion() -> None:
    mask_31 = (1 << 31) - 1
    mask_32 = (1 << 32) - 1
    first_31 = validator.CorrelationSignature(0, 0, 0, mask_31, mask_31)
    second_31 = validator.CorrelationSignature(0, 0, 0, mask_31, mask_31)
    first_32 = validator.CorrelationSignature(0, 0, 0, mask_32, mask_32)
    identical_32 = validator.CorrelationSignature(0, 0, 0, mask_32, mask_32)
    inverted_32 = validator.CorrelationSignature(0, 0, 0, 0, mask_32)

    assert validator._pair_projection_distance(first_31, second_31) is None
    assert validator._pair_projection_distance(first_32, identical_32) == 0
    assert validator._pair_projection_distance(first_32, inverted_32) == 0


def test_legacy_pack_without_quality_metadata_never_decodes_startup_synth(
    tmp_path: Path, fake_probe: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    sources = payload["sources"]
    assets = payload["assets"]
    assert isinstance(sources, list)
    assert isinstance(assets, list)
    sources.append(_source("legacy_keys", source_sha256="b" * 64))
    assets.append(
        _asset(
            pack_dir,
            "compat.startup-synth",
            "sfx/startup_synth.mp3",
            ["legacy_keys"],
            kind="compatibility",
            tags=[],
        )
    )
    _write_manifest(pack_dir, payload)
    decode_calls: list[Path] = []

    def decode(path: Path) -> list[int]:
        decode_calls.append(path)
        return _pcm_samples()

    monkeypatch.setattr(validator, "_decode_audio", decode)

    validator.validate_audio_asset_pack(pack_dir)
    assert decode_calls == []


def test_startup_synth_rejects_forbidden_tokens_across_source_metadata(tmp_path: Path, fake_probe: None) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    sources = payload["sources"]
    assets = payload["assets"]
    assert isinstance(sources, list)
    assert isinstance(assets, list)
    sources.append(
        _source(
            "trumpet_master",
            title="Brass Rhodes flourish",
            tags=["synth", "trumpet"],
            modification="Processed as a brass-backed electronic sting.",
        )
    )
    assets.append(
        _asset(
            pack_dir,
            "compat.startup-synth",
            "sfx/startup_synth.mp3",
            ["trumpet_master"],
            kind="compatibility",
            tags=["legacy", "synth"],
        )
    )
    _enable_quality_contract(payload)
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError) as caught:
        validator.validate_audio_asset_pack(pack_dir)
    message = str(caught.value)
    assert "cannot use brass/trumpet source 'trumpet_master'" in message
    assert "id=trumpet" in message
    assert "title=brass" in message
    assert "tags=trumpet" in message
    assert "modification=brass" in message


def test_startup_synth_rejects_forbidden_asset_tag(tmp_path: Path, fake_probe: None) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    sources = payload["sources"]
    assets = payload["assets"]
    assert isinstance(sources, list)
    assert isinstance(assets, list)
    sources.append(
        _source(
            "rhodes_keys",
            source_sha256="b" * 64,
            title="Yamaha Rhodes electric piano",
            tags=["rhodes"],
        )
    )
    assets.append(
        _asset(
            pack_dir,
            "compat.startup-synth",
            "sfx/startup_synth.mp3",
            ["rhodes_keys"],
            kind="compatibility",
            tags=["synth", "brass"],
        )
    )
    _enable_quality_contract(payload)
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError, match="cannot declare brass/trumpet semantics"):
        validator.validate_audio_asset_pack(pack_dir)


@pytest.mark.parametrize(
    ("title", "modification"),
    [
        ("Piano in E major", "Trimmed for the station."),
        ("Electric guitar with acoustic piano", "Trimmed for the station."),
        ("Electric guitar", "Layered with acoustic piano."),
    ],
)
def test_startup_synth_requires_contiguous_semantics_in_one_metadata_value(
    tmp_path: Path,
    fake_probe: None,
    title: str,
    modification: str,
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    sources = payload["sources"]
    assets = payload["assets"]
    assert isinstance(sources, list)
    assert isinstance(assets, list)
    sources.append(
        _source(
            "night_keys",
            source_sha256="b" * 64,
            title=title,
            modification=modification,
            tags=["keyboard"],
        )
    )
    assets.append(
        _asset(
            pack_dir,
            "compat.startup-synth",
            "sfx/startup_synth.mp3",
            ["night_keys"],
            kind="compatibility",
            tags=["synth"],
        )
    )
    _enable_quality_contract(payload)
    _write_manifest(pack_dir, payload)

    with pytest.raises(
        validator.AudioAssetPackValidationError,
        match="must reference at least one source with synth/e-piano/Rhodes semantics",
    ):
        validator.validate_audio_asset_pack(pack_dir)


def test_startup_synth_requires_asset_and_source_semantics_and_rejects_tag_relabeling(
    tmp_path: Path, fake_probe: None
) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    payload = _manifest(pack_dir)
    sources = payload["sources"]
    assets = payload["assets"]
    assert isinstance(sources, list)
    assert isinstance(assets, list)
    sources.append(_source("night_keys", title="Trumpet fanfare", tags=["ambience"]))
    assets.append(
        _asset(
            pack_dir,
            "compat.startup-synth",
            "sfx/startup_synth.mp3",
            ["night_keys"],
            kind="compatibility",
            tags=["legacy"],
        )
    )
    _enable_quality_contract(payload)
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError) as caught:
        validator.validate_audio_asset_pack(pack_dir)
    message = str(caught.value)
    assert "must include a semantic synth tag" in message
    assert "cannot use brass/trumpet source 'night_keys'" in message
    assert "must reference at least one source with synth/e-piano/Rhodes semantics" in message

    source = sources[-1]
    startup = assets[-1]
    assert isinstance(source, dict)
    assert isinstance(startup, dict)
    source["tags"] = ["electric-piano", "rhodes"]
    startup["tags"] = ["legacy", "synth"]
    _write_manifest(pack_dir, payload)

    with pytest.raises(validator.AudioAssetPackValidationError) as caught:
        validator.validate_audio_asset_pack(pack_dir)
    assert "cannot use brass/trumpet source 'night_keys'" in str(caught.value)

    source["title"] = "Yamaha Rhodes electric piano"
    _write_manifest(pack_dir, payload)
    validator.validate_audio_asset_pack(pack_dir)

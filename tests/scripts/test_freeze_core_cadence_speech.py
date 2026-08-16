"""Provider-free contract tests for the core cadence speech freezer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from scripts import freeze_core_cadence_speech as freezer

IDENTITY_DIGEST = "a" * 64


def test_json_copy_accepts_recursively_frozen_identity_metadata() -> None:
    frozen = MappingProxyType(
        {
            "provider": "elevenlabs",
            "settings": MappingProxyType(
                {
                    "stability": 0.6,
                    "nested": MappingProxyType({"speaker_boost": True}),
                }
            ),
        }
    )

    copied = freezer._json_copy(frozen)

    assert copied == {
        "provider": "elevenlabs",
        "settings": {
            "stability": 0.6,
            "nested": {"speaker_boost": True},
        },
    }
    assert isinstance(copied, dict)
    assert isinstance(copied["settings"], dict)


def test_runtime_promo_resolves_canonical_config_and_producer_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_load_config = freezer.load_config
    loaded_paths: list[str] = []

    def tracked_load_config(path: str):
        loaded_paths.append(path)
        return real_load_config(path)

    monkeypatch.setattr(freezer, "load_config", tracked_load_config)
    promo = freezer._resolve_runtime_promo()

    assert loaded_paths == [str(freezer.RUNTIME_CONFIG_PATH.resolve())]
    assert promo.config_path == freezer.RUNTIME_CONFIG_PATH.resolve()
    assert promo.config_sha256 == hashlib.sha256(freezer.RUNTIME_CONFIG_PATH.read_bytes()).hexdigest()
    assert promo.selector == "mammamiradio.scheduling.producer._safe_ad_promo_voice"
    assert promo.selected_voice_name == "L'Annunciatore"
    assert promo.route == {
        "provider": "elevenlabs",
        "voice": "QttbagfgqUCm9K0VgUyT",
        "model": "eleven_multilingual_v2",
        "settings": {
            "stability": 0.42,
            "similarity_boost": 0.78,
            "style": 0.45,
            "use_speaker_boost": True,
        },
    }


def test_generated_at_allows_small_clock_skew_but_rejects_impossible_future() -> None:
    now = datetime(2026, 8, 15, 22, 0, 0, tzinfo=UTC)

    assert freezer._stamp("20260815T220500Z", now=now) == "20260815T220500Z"
    with pytest.raises(ValueError, match="300 seconds in the future"):
        freezer._stamp("20260815T220501Z", now=now)


def test_manifest_builder_rejects_future_generated_at_before_material_use(tmp_path: Path) -> None:
    approved = freezer.ApprovedIdentity(tmp_path / "not-read.json", "a" * 64, {}, {})
    promo = freezer.RuntimePromo(
        config_path=tmp_path / "not-read.toml",
        config_sha256="b" * 64,
        selector=freezer.RUNTIME_PROMO_SELECTOR,
        selected_voice_name="Not read",
        route={},
    )
    impossible_future = (datetime.now(UTC) + timedelta(days=365)).strftime("%Y%m%dT%H%M%SZ")

    with pytest.raises(ValueError, match="300 seconds in the future"):
        freezer._build_manifest(approved, promo, [], generated_at=impossible_future)


def _route(
    provider: str,
    voice: str,
    model: str,
    settings: Mapping[str, object],
) -> dict[str, object]:
    effective = {
        "provider": provider,
        "voice": voice,
        "model": model,
        "settings": dict(settings),
    }
    return {
        "requested": deepcopy(effective),
        "effective": effective,
        "fallback_used": False,
    }


def _approved_metadata(identity_dir: Path) -> tuple[dict[str, Path], dict[str, dict[str, object]]]:
    routes = {
        "identity-isabella": _route(
            "azure",
            "it-IT-Isabella:DragonHDLatestNeural",
            "azure_speech",
            {
                "pitch": "+0Hz",
                "provider_output_format": "audio-24khz-160kbitrate-mono-mp3",
                "rate": "+0%",
            },
        ),
        "identity-marco": _route(
            "elevenlabs",
            "marco-approved-voice",
            "eleven_multilingual_v2",
            {
                "similarity_boost": 0.78,
                "stability": 0.6,
                "style": 0.45,
                "use_speaker_boost": True,
            },
        ),
        "identity-giulia": _route(
            "elevenlabs",
            "giulia-approved-voice",
            "eleven_multilingual_v2",
            {
                "similarity_boost": 0.78,
                "stability": 0.42,
                "style": 0.45,
                "use_speaker_boost": True,
            },
        ),
    }
    paths: dict[str, Path] = {}
    metadata: dict[str, dict[str, object]] = {}
    for source_voice_id, route in routes.items():
        path = identity_dir / f"{source_voice_id}.mp3"
        path.write_bytes(f"approved-source:{source_voice_id}".encode())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        paths[source_voice_id] = path
        metadata[source_voice_id] = {
            "id": source_voice_id,
            "audio_sha256": digest,
            "route": route,
        }
    return paths, metadata


def _patch_approved_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, Path], dict[str, dict[str, object]]]:
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    manifest_path = identity_dir / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    paths, metadata = _approved_metadata(identity_dir)

    def approved_identity(requested_path: Path):
        assert requested_path == manifest_path.resolve()
        return IDENTITY_DIGEST, paths, metadata

    monkeypatch.setattr(freezer, "approved_identity_clips_by_id", approved_identity)
    return manifest_path, paths, metadata


def _enable_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SPEECH_KEY", "test-azure-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "test-region")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-elevenlabs-key")


def _patch_audio_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freezer, "_probe_audio", lambda _path: (dict(freezer.FORMAT), 1.375))


def _patch_providers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_elevenlabs_call: int | None = None,
) -> tuple[list[tuple[str, str, str, Path, dict[str, object]]], list[tuple[str, str, str, Path, dict[str, object]]]]:
    azure_calls: list[tuple[str, str, str, Path, dict[str, object]]] = []
    elevenlabs_calls: list[tuple[str, str, str, Path, dict[str, object]]] = []

    async def fake_azure(text: str, voice: str, output_path: Path, **kwargs: object) -> Path:
        azure_calls.append(("azure", text, voice, output_path, dict(kwargs)))
        output_path.write_bytes(f"azure:{text}:{voice}".encode())
        return output_path

    async def fake_elevenlabs(text: str, voice: str, output_path: Path, **kwargs: object) -> Path:
        elevenlabs_calls.append(("elevenlabs", text, voice, output_path, dict(kwargs)))
        if fail_elevenlabs_call is not None and len(elevenlabs_calls) == fail_elevenlabs_call:
            raise RuntimeError("simulated ElevenLabs failure")
        output_path.write_bytes(f"elevenlabs:{text}:{voice}".encode())
        return output_path

    monkeypatch.setattr(freezer.tts_module, "synthesize_azure", fake_azure)
    monkeypatch.setattr(freezer.tts_module, "synthesize_elevenlabs", fake_elevenlabs)
    return azure_calls, elevenlabs_calls


async def _render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    output_name: str = "speech-freeze",
) -> tuple[Path, dict[str, object], list[Any], list[Any]]:
    identity_manifest, _paths, _metadata = _patch_approved_identity(tmp_path, monkeypatch)
    _enable_credentials(monkeypatch)
    _patch_audio_probe(monkeypatch)
    azure_calls, elevenlabs_calls = _patch_providers(monkeypatch)
    output_dir = tmp_path / output_name
    manifest = await freezer.freeze_core_cadence_speech(
        identity_manifest,
        output_dir,
        generated_at="20260815T010203Z",
    )
    return output_dir, manifest, azure_calls, elevenlabs_calls


@pytest.mark.asyncio
async def test_freeze_binds_exact_routes_copy_hashes_and_no_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir, manifest, azure_calls, elevenlabs_calls = await _render(tmp_path, monkeypatch)

    assert freezer.REQUIRED_CLIP_IDS == (
        "speech.sweeper",
        "speech.promo",
        "speech.spot-a",
        "speech.spot-b",
    )
    assert manifest["schema_version"] == 1
    assert manifest["stage"] == freezer.STAGE
    assert manifest["release_ready"] is False
    assert manifest["runtime_promo"] == {
        "config_path": "radio.toml",
        "config_path_kind": "repo-relative",
        "config_sha256": hashlib.sha256(freezer.RUNTIME_CONFIG_PATH.read_bytes()).hexdigest(),
        "selector": "mammamiradio.scheduling.producer._safe_ad_promo_voice",
        "selected_voice_name": "L'Annunciatore",
        "route": {
            "provider": "elevenlabs",
            "voice": "QttbagfgqUCm9K0VgUyT",
            "model": "eleven_multilingual_v2",
            "settings": {
                "stability": 0.42,
                "similarity_boost": 0.78,
                "style": 0.45,
                "use_speaker_boost": True,
            },
        },
    }
    assert manifest["provider_contract"] == {
        "direct_helpers": {
            "azure": "mammamiradio.audio.tts.synthesize_azure",
            "elevenlabs": "mammamiradio.audio.tts.synthesize_elevenlabs",
        },
        "fallback_permitted": False,
        "generic_synthesize_permitted": False,
        "credentials_recorded": False,
    }

    clips = manifest["clips"]
    assert isinstance(clips, list)
    assert [clip["id"] for clip in clips] == list(freezer.REQUIRED_CLIP_IDS)
    assert [clip["text"] for clip in clips] == [spec.text for spec in freezer.SPEECH_SPECS]
    assert all(clip["text_sha256"] == freezer._text_sha256(clip["text"]) for clip in clips)
    assert all(clip["format"] == freezer.FORMAT for clip in clips)
    assert len({clip["audio_sha256"] for clip in clips}) == 4
    assert [(clip["source_kind"], clip["source_voice_id"]) for clip in clips] == [
        ("approved-identity", "identity-isabella"),
        ("runtime-promo-selection", None),
        ("approved-identity", "identity-marco"),
        ("approved-identity", "identity-giulia"),
    ]

    assert len(azure_calls) == 1
    _provider, text, voice, _path, azure_kwargs = azure_calls[0]
    assert text == "Sei su Mamma Mi Radio."
    assert voice == "it-IT-Isabella:DragonHDLatestNeural"
    assert azure_kwargs == {"rate": "+0%", "pitch": "+0Hz", "loudnorm": True}

    assert [call[1] for call in elevenlabs_calls] == [
        "A word from our sponsors, amici.",
        "Caffè Aurora: il gusto caldo che accompagna la notte. Disponibile vicino a te.",
        "Luce Notturna: una lampada discreta per le ore più tranquille. Accendila con calma.",
    ]
    assert [call[2] for call in elevenlabs_calls] == [
        "QttbagfgqUCm9K0VgUyT",
        "marco-approved-voice",
        "giulia-approved-voice",
    ]
    assert all("rate" not in call[4] and "pitch" not in call[4] for call in elevenlabs_calls)
    assert elevenlabs_calls[0][4]["voice_settings"] == {
        "stability": 0.42,
        "similarity_boost": 0.78,
        "style": 0.45,
        "use_speaker_boost": True,
    }
    assert elevenlabs_calls[1][4]["voice_settings"]["stability"] == 0.6
    assert elevenlabs_calls[2][4]["voice_settings"]["stability"] == 0.42

    promo = clips[1]
    assert promo["character"] == "L'Annunciatore"
    assert promo["source_kind"] == "runtime-promo-selection"
    assert promo["source_voice_id"] is None
    assert promo["source_voice_audio_sha256"] is None
    runtime_promo = manifest["runtime_promo"]
    assert isinstance(runtime_promo, dict)
    assert {key: promo[key] for key in ("provider", "voice", "model", "settings")} == runtime_promo["route"]
    assert promo["prosody"] == {
        "requested_rate": "+40%",
        "requested_pitch": "-10Hz",
        "applied": False,
        "reason": "direct ElevenLabs helper exposes no rate or pitch transform; no transform applied",
    }
    assert manifest["pack_digest"] == freezer._pack_digest(manifest)
    assert (output_dir / ".ready").read_text(encoding="ascii").strip() == manifest["pack_digest"]

    validated, paths_by_id = freezer.validate_core_cadence_speech_manifest(output_dir / "manifest.json")
    assert validated == manifest
    assert tuple(paths_by_id) == freezer.REQUIRED_CLIP_IDS
    assert {path.name for path in paths_by_id.values()} == {spec.filename for spec in freezer.SPEECH_SPECS}


@pytest.mark.asyncio
async def test_manifest_is_deterministic_for_identical_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_manifest, _paths, _metadata = _patch_approved_identity(tmp_path, monkeypatch)
    _enable_credentials(monkeypatch)
    _patch_audio_probe(monkeypatch)
    _patch_providers(monkeypatch)

    manifests = []
    for name in ("one", "two"):
        manifests.append(
            await freezer.freeze_core_cadence_speech(
                identity_manifest,
                tmp_path / name,
                generated_at="20260815T010203Z",
            )
        )

    assert manifests[0] == manifests[1]
    assert (tmp_path / "one" / "manifest.json").read_bytes() == (tmp_path / "two" / "manifest.json").read_bytes()


@pytest.mark.asyncio
async def test_unapproved_identity_fails_before_any_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_manifest = tmp_path / "identity.json"
    identity_manifest.write_text("{}\n", encoding="utf-8")
    calls: list[str] = []

    def rejected(_path: Path):
        raise ValueError("Identity listening gate is not approved for this pack digest")

    async def should_not_render(*_args: object, **_kwargs: object) -> Path:
        calls.append("provider")
        raise AssertionError("provider should not be called")

    monkeypatch.setattr(freezer, "approved_identity_clips_by_id", rejected)
    monkeypatch.setattr(freezer.tts_module, "synthesize_azure", should_not_render)
    monkeypatch.setattr(freezer.tts_module, "synthesize_elevenlabs", should_not_render)

    with pytest.raises(ValueError, match="not approved"):
        await freezer.freeze_core_cadence_speech(identity_manifest, tmp_path / "rejected")
    assert calls == []
    assert not (tmp_path / "rejected").exists()


@pytest.mark.asyncio
async def test_future_generated_at_fails_before_sources_or_providers_are_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def should_not_resolve(_path: Path):
        calls.append("identity")
        raise AssertionError("identity should not be resolved")

    async def should_not_render(*_args: object, **_kwargs: object) -> Path:
        calls.append("provider")
        raise AssertionError("provider should not be called")

    monkeypatch.setattr(freezer, "approved_identity_clips_by_id", should_not_resolve)
    monkeypatch.setattr(freezer.tts_module, "synthesize_azure", should_not_render)
    monkeypatch.setattr(freezer.tts_module, "synthesize_elevenlabs", should_not_render)
    impossible_future = (datetime.now(UTC) + timedelta(days=365)).strftime("%Y%m%dT%H%M%SZ")

    with pytest.raises(ValueError, match="300 seconds in the future"):
        await freezer.freeze_core_cadence_speech(
            tmp_path / "not-read.json",
            tmp_path / "future",
            generated_at=impossible_future,
        )
    assert calls == []
    assert not (tmp_path / "future").exists()


@pytest.mark.asyncio
async def test_effective_route_mismatch_fails_closed_before_provider_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_manifest, _paths, metadata = _patch_approved_identity(tmp_path, monkeypatch)
    _enable_credentials(monkeypatch)
    route = metadata["identity-marco"]["route"]
    assert isinstance(route, dict)
    effective = route["effective"]
    assert isinstance(effective, dict)
    effective["voice"] = "not-the-approved-requested-voice"
    calls: list[str] = []

    async def should_not_render(*_args: object, **_kwargs: object) -> Path:
        calls.append("provider")
        raise AssertionError("provider should not be called")

    monkeypatch.setattr(freezer.tts_module, "synthesize_azure", should_not_render)
    monkeypatch.setattr(freezer.tts_module, "synthesize_elevenlabs", should_not_render)

    with pytest.raises(ValueError, match="requested and effective routes differ"):
        await freezer.freeze_core_cadence_speech(identity_manifest, tmp_path / "wrong-route")
    assert calls == []


@pytest.mark.asyncio
async def test_provider_failure_leaves_no_partial_or_claim_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_manifest, _paths, _metadata = _patch_approved_identity(tmp_path, monkeypatch)
    _enable_credentials(monkeypatch)
    _patch_audio_probe(monkeypatch)
    _patch_providers(monkeypatch, fail_elevenlabs_call=1)
    output_dir = tmp_path / "atomic-failure"

    with pytest.raises(RuntimeError, match="simulated ElevenLabs failure"):
        await freezer.freeze_core_cadence_speech(
            identity_manifest,
            output_dir,
            generated_at="20260815T010203Z",
        )

    assert not output_dir.exists()
    assert not (tmp_path / ".atomic-failure.claim").exists()
    assert not list(tmp_path.glob(".atomic-failure.*"))


@pytest.mark.asyncio
async def test_existing_output_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_manifest, _paths, _metadata = _patch_approved_identity(tmp_path, monkeypatch)
    _enable_credentials(monkeypatch)
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    marker = output_dir / "owned-by-user.txt"
    marker.write_text("keep\n", encoding="utf-8")
    calls: list[str] = []

    async def should_not_render(*_args: object, **_kwargs: object) -> Path:
        calls.append("provider")
        raise AssertionError("provider should not be called")

    monkeypatch.setattr(freezer.tts_module, "synthesize_azure", should_not_render)
    monkeypatch.setattr(freezer.tts_module, "synthesize_elevenlabs", should_not_render)

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        await freezer.freeze_core_cadence_speech(identity_manifest, output_dir)
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert calls == []


@pytest.mark.asyncio
async def test_validator_rejects_path_escape_even_with_recomputed_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir, _manifest, _azure_calls, _elevenlabs_calls = await _render(tmp_path, monkeypatch)
    manifest_path = output_dir / "manifest.json"
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["clips"][0]["path"] = "../escape.mp3"
    value["pack_digest"] = freezer._pack_digest(value)
    manifest_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / ".ready").write_text(value["pack_digest"] + "\n", encoding="ascii")

    with pytest.raises(ValueError, match="unsafe or duplicate path"):
        freezer.validate_core_cadence_speech_manifest(manifest_path)


@pytest.mark.asyncio
async def test_validator_rejects_stale_upstream_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir, _manifest, _azure_calls, _elevenlabs_calls = await _render(tmp_path, monkeypatch)
    original = freezer.approved_identity_clips_by_id

    def changed(path: Path):
        _digest, paths, metadata = original(path)
        return "b" * 64, paths, metadata

    monkeypatch.setattr(freezer, "approved_identity_clips_by_id", changed)
    with pytest.raises(ValueError, match="digest is stale"):
        freezer.validate_core_cadence_speech_manifest(output_dir / "manifest.json")


def _rewrite_manifest(output_dir: Path, value: dict[str, Any]) -> None:
    value["pack_digest"] = freezer._pack_digest(value)
    (output_dir / "manifest.json").write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / ".ready").write_text(str(value["pack_digest"]) + "\n", encoding="ascii")


@pytest.mark.asyncio
async def test_validator_rejects_invalid_recorded_runtime_config_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir, _manifest, _azure_calls, _elevenlabs_calls = await _render(tmp_path, monkeypatch)
    manifest_path = output_dir / "manifest.json"
    value: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["runtime_promo"]["config_sha256"] = "not-a-sha256"
    _rewrite_manifest(output_dir, value)

    with pytest.raises(ValueError, match="config hash is invalid"):
        freezer.validate_core_cadence_speech_manifest(manifest_path)


@pytest.mark.asyncio
async def test_validator_ignores_unrelated_runtime_config_bytes_when_promo_route_is_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir, manifest, _azure_calls, _elevenlabs_calls = await _render(tmp_path, monkeypatch)
    current = freezer._resolve_runtime_promo()
    drifted = freezer.RuntimePromo(
        config_path=current.config_path,
        config_sha256="b" * 64,
        selector=current.selector,
        selected_voice_name=current.selected_voice_name,
        route=current.route,
    )
    monkeypatch.setattr(freezer, "_resolve_runtime_promo", lambda: drifted)

    validated, _paths = freezer.validate_core_cadence_speech_manifest(output_dir / "manifest.json")

    assert validated["pack_digest"] == manifest["pack_digest"]
    runtime_promo = validated["runtime_promo"]
    assert isinstance(runtime_promo, dict)
    assert runtime_promo["config_sha256"] != drifted.config_sha256


@pytest.mark.asyncio
async def test_validator_rejects_current_runtime_promo_settings_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir, _manifest, _azure_calls, _elevenlabs_calls = await _render(tmp_path, monkeypatch)
    current = freezer._resolve_runtime_promo()
    changed_route = deepcopy(current.route)
    changed_settings = changed_route["settings"]
    assert isinstance(changed_settings, dict)
    changed_settings["stability"] = 0.5
    changed = freezer.RuntimePromo(
        config_path=current.config_path,
        config_sha256="b" * 64,
        selector=current.selector,
        selected_voice_name=current.selected_voice_name,
        route=changed_route,
    )
    monkeypatch.setattr(freezer, "_resolve_runtime_promo", lambda: changed)

    with pytest.raises(ValueError, match="promo route is stale"):
        freezer.validate_core_cadence_speech_manifest(output_dir / "manifest.json")


@pytest.mark.asyncio
async def test_validator_reresolves_runtime_promo_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir, _manifest, _azure_calls, _elevenlabs_calls = await _render(tmp_path, monkeypatch)

    def select_different_voice(voices: list[Any]):
        return voices[1]

    monkeypatch.setattr(freezer, "_safe_ad_promo_voice", select_different_voice)
    with pytest.raises(ValueError, match="promo selection is stale"):
        freezer.validate_core_cadence_speech_manifest(output_dir / "manifest.json")


@pytest.mark.asyncio
async def test_validator_rejects_mutated_runtime_promo_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir, _manifest, _azure_calls, _elevenlabs_calls = await _render(tmp_path, monkeypatch)
    manifest_path = output_dir / "manifest.json"
    value: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["runtime_promo"]["route"]["voice"] = "not-the-runtime-voice"
    _rewrite_manifest(output_dir, value)

    with pytest.raises(ValueError, match="promo route is stale"):
        freezer.validate_core_cadence_speech_manifest(manifest_path)


@pytest.mark.asyncio
async def test_validator_rejects_future_generated_at_even_with_fresh_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir, _manifest, _azure_calls, _elevenlabs_calls = await _render(tmp_path, monkeypatch)
    manifest_path = output_dir / "manifest.json"
    value: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["generated_at"] = (datetime.now(UTC) + timedelta(days=365)).strftime("%Y%m%dT%H%M%SZ")
    _rewrite_manifest(output_dir, value)

    with pytest.raises(ValueError, match="300 seconds in the future"):
        freezer.validate_core_cadence_speech_manifest(manifest_path)

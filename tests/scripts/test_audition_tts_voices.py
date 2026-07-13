from __future__ import annotations

import hashlib
import json

import pytest

from mammamiradio.core.config import (
    AdsSection,
    PacingSection,
    PlaylistSection,
    SonicBrandSection,
    StationConfig,
    StationSection,
)
from mammamiradio.core.models import HostPersonality, PersonalityAxes
from mammamiradio.hosts.ad_creative import AdVoice
from scripts import audition_tts_voices as audition


def _station_config() -> StationConfig:
    return StationConfig(
        station=StationSection(),
        playlist=PlaylistSection(),
        pacing=PacingSection(),
        hosts=[
            HostPersonality(
                name="Marco",
                voice="cedar",
                style="big host",
                personality=PersonalityAxes(energy=75, warmth=45, chaos=40),
                engine="openai",
                edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
            ),
            HostPersonality(
                name="Giulia",
                voice="it-IT-IsabellaNeural",
                style="dry cohost",
                personality=PersonalityAxes(energy=35, warmth=35, chaos=50),
                engine="edge",
            ),
        ],
        ads=AdsSection(
            voices=[
                AdVoice(
                    name="Roberto",
                    voice="it-IT-Alessio:DragonHDLatestNeural",
                    style="warm salesman",
                    engine="azure",
                    edge_fallback_voice="it-IT-DiegoNeural",
                ),
                AdVoice(
                    name="Rinaldo",
                    voice="it-IT-Alessio:DragonHDLatestNeural",
                    style="same timbre on purpose",
                    engine="azure",
                    edge_fallback_voice="it-IT-DiegoNeural",
                ),
            ]
        ),
        sonic_brand=SonicBrandSection(
            sweeper_voice="it-IT-IsabellaNeural",
            sweeper_engine="edge",
            sweeper_edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
        ),
    )


def _by_voice(targets: list[audition.VoiceAuditionTarget]) -> dict[tuple[str, str], audition.VoiceAuditionTarget]:
    return {(target.provider, target.voice): target for target in targets}


def test_build_audition_targets_dedupes_roles_and_catalog_voices() -> None:
    targets = audition.build_audition_targets(
        _station_config(),
        providers=["edge", "openai", "azure"],
        include_catalog=True,
    )
    by_voice = _by_voice(targets)

    cedar = by_voice[("openai", "cedar")]
    assert cedar.source == "configured+catalog"
    assert "host:Marco" in cedar.used_by
    assert "catalog:openai" in cedar.used_by
    assert cedar.edge_fallback_voice == "it-IT-GiuseppeMultilingualNeural"
    assert cedar.openai_instructions

    isabella = by_voice[("edge", "it-IT-IsabellaNeural")]
    assert "host:Giulia" in isabella.used_by
    assert "sonic_brand:sweeper" in isabella.used_by
    assert "catalog:edge" in isabella.used_by
    assert isabella.rate == "-10%"
    assert isabella.pitch == "+5Hz"

    alessio = by_voice[("azure", "it-IT-Alessio:DragonHDLatestNeural")]
    assert "ad:Roberto" in alessio.used_by
    assert "ad:Rinaldo" in alessio.used_by
    assert "catalog:azure" in alessio.used_by


def test_manual_voice_specs_support_provider_prefixed_ids_with_colons() -> None:
    specs = audition.parse_manual_voice_specs(
        [
            "openai:marin",
            "azure:it-IT-Isabella:DragonHDLatestNeural",
            "elevenlabs:voice-id-123",
        ]
    )

    assert specs == [
        ("openai", "marin"),
        ("azure", "it-IT-Isabella:DragonHDLatestNeural"),
        ("elevenlabs", "voice-id-123"),
    ]


def test_expand_stability_variants_fans_out_elevenlabs_only() -> None:
    targets = [
        audition.VoiceAuditionTarget(
            provider="elevenlabs",
            voice="v1",
            label="host-marco",
            source="configured",
            voice_settings={"style": 0.45},
        ),
        audition.VoiceAuditionTarget(
            provider="edge", voice="it-IT-DiegoNeural", label="catalog-edge", source="catalog"
        ),
    ]

    expanded = audition.expand_stability_variants(targets, [0.42, 0.6])

    by_label = {t.label: t for t in expanded}
    # Edge target passes through untouched; ElevenLabs fans out into one clip per stability.
    assert by_label["catalog-edge"].voice_settings is None
    # Pre-existing voice_settings (style) are preserved; stability is merged in, not overwritten.
    assert by_label["host-marco-stab42"].voice_settings == {"style": 0.45, "stability": 0.42}
    assert by_label["host-marco-stab60"].voice_settings == {"style": 0.45, "stability": 0.6}
    # The voice id is preserved on every variant — only the settings/label differ.
    assert all(t.voice == "v1" for t in expanded if t.provider == "elevenlabs")


def test_expand_stability_variants_noop_when_empty() -> None:
    targets = [audition.VoiceAuditionTarget(provider="elevenlabs", voice="v1", label="m", source="configured")]
    assert audition.expand_stability_variants(targets, None) is targets
    assert audition.expand_stability_variants(targets, []) is targets


def test_configured_ad_targets_keep_their_selected_voice_settings() -> None:
    config = _station_config()
    tiziana = AdVoice(
        name="Tiziana",
        voice="RXoaSpLaWTEckJgPUBG3",
        style="balanced and credible",
        engine="elevenlabs",
        edge_fallback_voice="it-IT-IsabellaNeural",
    )
    # This assignment works before and after the config dataclass grows the
    # field, so the operator tool's contract is protected independently.
    tiziana.voice_settings = {"stability": 0.6, "style": 0.2}
    config.ads.voices.append(tiziana)

    targets = audition.collect_configured_targets(config)

    target = next(target for target in targets if target.label == "ad-Tiziana")
    assert target.provider == "elevenlabs"
    assert target.voice_settings == {"stability": 0.6, "style": 0.2}


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_target_uses_its_configured_voice_settings(tmp_path, monkeypatch) -> None:
    calls: list[dict | None] = []

    async def fake_elevenlabs(text, voice, output_path, *, voice_settings=None, **_kwargs):
        calls.append(voice_settings)
        output_path.write_bytes(b"fake mp3")
        return output_path

    monkeypatch.setattr(audition.tts_module, "synthesize_elevenlabs", fake_elevenlabs)
    target = audition.VoiceAuditionTarget(
        provider="elevenlabs",
        voice="RXoaSpLaWTEckJgPUBG3",
        label="ad-tiziana",
        source="configured",
        voice_settings={"stability": 0.6, "style": 0.2},
    )

    output = await audition._synthesize_target(target, tmp_path / "tiziana.mp3")

    assert output.read_bytes() == b"fake mp3"
    assert calls == [{"stability": 0.6, "style": 0.2}]


def test_stability_arg_validates_range_and_finiteness() -> None:
    import argparse

    assert audition._stability_arg("0.42") == 0.42
    assert audition._stability_arg("0") == 0.0
    assert audition._stability_arg("1") == 1.0
    # Out-of-range and non-finite values fail at parse time with a clear CLI error.
    for bad in ("1.2", "-0.1", "42", "inf", "nan"):
        with pytest.raises(argparse.ArgumentTypeError):
            audition._stability_arg(bad)


def test_missing_env_for_provider_is_secret_safe() -> None:
    env = {"OPENAI_API_KEY": "sk-test", "AZURE_SPEECH_KEY": "azure-key"}

    assert audition.missing_env_for_provider("edge", env) == ()
    assert audition.missing_env_for_provider("openai", env) == ()
    assert audition.missing_env_for_provider("azure", env) == ("AZURE_SPEECH_REGION",)
    assert audition.missing_env_for_provider("elevenlabs", env) == ("ELEVENLABS_API_KEY",)


def test_missing_env_for_provider_honors_explicit_empty_env(monkeypatch) -> None:
    """An explicitly-empty env means 'no credentials', even when the process env
    has them — `env or os.environ` used to leak os.environ for a falsy `{}`."""
    monkeypatch.setenv("AZURE_SPEECH_KEY", "leaked")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "leaked")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "leaked")

    assert audition.missing_env_for_provider("azure", {}) == ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION")
    assert audition.missing_env_for_provider("elevenlabs", {}) == ("ELEVENLABS_API_KEY",)
    # None (the default) still falls back to the process environment.
    assert audition.missing_env_for_provider("azure", None) == ()


@pytest.mark.asyncio
async def test_run_auditions_skips_missing_cloud_credentials_without_synthesizing(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_synthesize(target: audition.VoiceAuditionTarget, output_path):
        calls.append((target.provider, target.voice))
        output_path.write_bytes(b"fake mp3")
        return output_path

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    targets = [
        audition.VoiceAuditionTarget(provider="openai", voice="cedar", label="openai", source="test"),
        audition.VoiceAuditionTarget(
            provider="edge",
            voice="it-IT-IsabellaNeural",
            label="edge",
            source="test",
        ),
    ]

    results = await audition.run_auditions(targets, tmp_path, env={}, dry_run=False)

    assert [result.status for result in results] == [audition.STATUS_SKIPPED, audition.STATUS_GENERATED]
    assert results[0].missing_env == ("OPENAI_API_KEY",)
    assert calls == [("edge", "it-IT-IsabellaNeural")]
    assert (tmp_path / "02-edge-it-IT-IsabellaNeural.mp3").read_bytes() == b"fake mp3"


@pytest.mark.asyncio
async def test_run_auditions_strict_missing_credentials_are_failures(tmp_path, monkeypatch) -> None:
    async def fail_if_called(_target: audition.VoiceAuditionTarget, _output_path):
        raise AssertionError("missing-key audition should not synthesize")

    monkeypatch.setattr(audition, "_synthesize_target", fail_if_called)

    results = await audition.run_auditions(
        [audition.VoiceAuditionTarget(provider="azure", voice="it-IT-DiegoNeural", label="azure", source="test")],
        tmp_path,
        env={},
        strict=True,
    )

    assert results[0].status == audition.STATUS_FAILED
    assert results[0].missing_env == ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION")


def test_write_manifest_records_results_without_secret_values(tmp_path) -> None:
    result = audition.VoiceAuditionResult(
        provider="openai",
        voice="cedar",
        label="openai",
        source="test",
        used_by=("catalog:openai",),
        status=audition.STATUS_SKIPPED,
        missing_env=("OPENAI_API_KEY",),
    )

    manifest = audition.write_manifest(
        [result],
        tmp_path,
        config_path=tmp_path / "radio.toml",
        timestamp="20260603T120000Z",
    )

    payload = json.loads(manifest.read_text())
    assert payload["counts"] == {audition.STATUS_SKIPPED: 1}
    assert payload["results"][0]["missing_env"] == ["OPENAI_API_KEY"]
    assert "sk-test" not in manifest.read_text()
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        audition.write_manifest([result], tmp_path, config_path=tmp_path / "radio.toml", timestamp="20260603T120000Z")


def _selection_candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "candidate_id": "RXoaSpLaWTEckJgPUBG3",
        "candidate_name": "Dottoressa Tiziana",
        "profile": {
            "engine": "elevenlabs",
            "model": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.6, "style": 0.2, "use_speaker_boost": True},
        },
        "text_sha256": "a" * 64,
        "provider_result": "generated",
        "audio_sha256": "b" * 64,
        "audio_duration_seconds": 7.25,
        "approval_status": "accepted",
        "rationale": "accepted_balanced_brand_fit",
    }
    candidate.update(overrides)
    return candidate


def test_selection_receipt_writer_keeps_only_complete_safe_evidence(tmp_path) -> None:
    receipt_path = tmp_path / "proof" / "selection.json"

    written = audition.write_selection_receipt([_selection_candidate()], path=receipt_path)

    assert written == receipt_path
    payload = json.loads(receipt_path.read_text())
    audition.validate_selection_receipt(payload)
    assert set(payload) == {"schema_version", "candidates"}
    assert payload["candidates"][0]["text_sha256"] == "a" * 64
    assert "audio_path" not in receipt_path.read_text()
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        audition.write_selection_receipt([_selection_candidate()], path=receipt_path)


@pytest.mark.parametrize(
    ("extra_field", "value"),
    [
        ("text", "Questa e la copia integrale che non deve finire nel receipt"),
        ("audio_path", "/tmp/voice-auditions/tiziana.mp3"),
        ("credentials", "elevenlabs-secret"),
    ],
)
def test_selection_receipt_rejects_raw_copy_paths_and_credentials(extra_field: str, value: str) -> None:
    candidate = _selection_candidate(**{extra_field: value})

    with pytest.raises(ValueError, match="prohibited fields"):
        audition.selection_receipt([candidate])


@pytest.mark.parametrize(
    "rationale",
    [
        "Questa offerta arriva adesso e la casa respira piano mentre tutti restano qui.",
        "sk-example-secret-token",
    ],
)
def test_selection_receipt_rejects_freeform_copy_or_credentials_in_rationale(rationale: str) -> None:
    with pytest.raises(ValueError, match="controlled rationale code"):
        audition.selection_receipt([_selection_candidate(rationale=rationale)])


def test_selection_receipt_rejects_incomplete_generated_evidence() -> None:
    candidate = _selection_candidate(audio_sha256=None)

    with pytest.raises(ValueError, match="needs audio checksum and duration"):
        audition.selection_receipt([candidate])


def test_selection_receipt_from_manifest_keeps_human_decision_and_redacts_local_evidence(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"
    decisions_path = tmp_path / "decisions.json"
    receipt_path = tmp_path / "proof" / "selection.json"
    manifest_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "voice": "RXoaSpLaWTEckJgPUBG3",
                        "used_by": ["ad:Dottoressa Tiziana"],
                        "status": "generated",
                        "output_path": "/tmp/voice-auditions/tiziana.mp3",
                        "profile": {
                            "engine": "elevenlabs",
                            "model": "eleven_multilingual_v2",
                            "voice_settings": {
                                "stability": 0.42,
                                "similarity_boost": 0.78,
                                "style": 0.45,
                                "use_speaker_boost": True,
                            },
                        },
                        "text_sha256": "a" * 64,
                        "audio_sha256": "b" * 64,
                        "audio_duration_seconds": 7.25,
                    }
                ]
            }
        )
    )
    decisions_path.write_text(
        json.dumps(
            [
                {
                    "candidate_id": "RXoaSpLaWTEckJgPUBG3",
                    "candidate_name": "Dottoressa Tiziana",
                    "approval_status": "accepted",
                    "rationale": "accepted_balanced_brand_fit",
                }
            ]
        )
    )

    written = audition.write_selection_receipt_from_manifest(
        manifest_path=manifest_path,
        decisions_path=decisions_path,
        path=receipt_path,
    )

    assert written == receipt_path
    payload = audition.load_selection_receipt(receipt_path)
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    assert candidate["candidate_name"] == "Dottoressa Tiziana"
    assert "/tmp/voice-auditions" not in receipt_path.read_text()


@pytest.mark.asyncio
async def test_generated_manifest_evidence_uses_the_exact_eleven_v2_profile(tmp_path, monkeypatch) -> None:
    async def fake_synthesize(target: audition.VoiceAuditionTarget, output_path):
        output_path.write_bytes(b"rendered")
        return output_path

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    monkeypatch.setattr(audition, "probe_duration_sec", lambda _path: 8.0)
    target = audition.VoiceAuditionTarget(
        provider="elevenlabs",
        voice="RXoaSpLaWTEckJgPUBG3",
        label="ad-Dottoressa-Tiziana",
        source="configured",
        used_by=("ad:Dottoressa Tiziana",),
        text="Una prova di audizione.",
    )

    [result] = await audition.run_auditions([target], tmp_path, env={"ELEVENLABS_API_KEY": "test"})

    assert result.status == audition.STATUS_GENERATED
    assert result.text_sha256 == hashlib.sha256(target.text.encode("utf-8")).hexdigest()
    assert result.audio_sha256 == hashlib.sha256(b"rendered").hexdigest()
    assert result.audio_duration_seconds == 8.0
    assert result.profile == {
        "engine": "elevenlabs",
        "model": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.42, "similarity_boost": 0.78, "style": 0.45, "use_speaker_boost": True},
    }


def test_committed_selection_receipt_is_valid_when_human_approval_adds_it() -> None:
    """The reviewed proof is intentionally absent until real provider/human gates run.

    Once that tracked receipt exists, CI validates its full safe schema without
    making a provider call or trying to infer an approval result.
    """
    if audition.SELECTION_RECEIPT_PATH.exists():
        audition.load_selection_receipt()


def test_cli_dry_run_lists_all_openai_catalog_without_writing_files(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(audition, "load_config", lambda _path: _station_config())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    rc = audition.main(
        [
            "--providers",
            "openai",
            "--include-catalog",
            "--dry-run",
            "--output-dir",
            str(tmp_path),
            "--timestamp",
            "20260603T120000Z",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "Dry-run targets:" in captured.out
    assert "planned\topenai\tcedar" in captured.out
    assert "planned\topenai\tmarin" in captured.out
    assert list(tmp_path.iterdir()) == []


def test_cli_rejects_bad_manual_voice_spec(tmp_path, capsys) -> None:
    rc = audition.main(["--voice", "cedar", "--output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 2
    assert "provider:voice_id" in captured.err

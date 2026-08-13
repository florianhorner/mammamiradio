from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
            "voice_settings": {
                "stability": 0.6,
                "similarity_boost": 0.78,
                "style": 0.2,
                "use_speaker_boost": True,
            },
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


@pytest.mark.parametrize(
    ("profile", "match"),
    [
        (
            {
                "engine": "elevenlabs",
                "model": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.6, "style": 0.2, "use_speaker_boost": True},
            },
            "voice_settings is missing fields: similarity_boost",
        ),
        (
            {
                "engine": "elevenlabs",
                "model": "eleven_turbo_v2_5",
                "voice_settings": {
                    "stability": 0.6,
                    "similarity_boost": 0.78,
                    "style": 0.2,
                    "use_speaker_boost": True,
                },
            },
            "model must be 'eleven_multilingual_v2'",
        ),
    ],
)
def test_selection_receipt_rejects_incomplete_or_wrong_eleven_v2_profile(
    profile: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        audition.selection_receipt([_selection_candidate(profile=profile)])


def test_selection_receipt_from_manifest_keeps_human_decision_and_redacts_local_evidence(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"
    decisions_path = tmp_path / "decisions.json"
    receipt_path = tmp_path / "proof" / "selection.json"
    profile = {
        "engine": "elevenlabs",
        "model": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.42,
            "similarity_boost": 0.78,
            "style": 0.45,
            "use_speaker_boost": True,
        },
    }
    candidate_id = audition._selection_candidate_id("elevenlabs", "RXoaSpLaWTEckJgPUBG3", profile)
    manifest_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "provider": "elevenlabs",
                        "voice": "RXoaSpLaWTEckJgPUBG3",
                        "candidate_id": candidate_id,
                        "used_by": ["ad:Dottoressa Tiziana"],
                        "status": "generated",
                        "output_path": "/tmp/voice-auditions/tiziana.mp3",
                        "profile": profile,
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
                    "candidate_id": candidate_id,
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


def test_selection_receipt_from_manifest_keeps_same_voice_profile_variants_distinct(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"
    decisions_path = tmp_path / "decisions.json"
    receipt_path = tmp_path / "proof" / "selection.json"
    base_voice_settings: dict[str, object] = {
        "stability": 0.42,
        "similarity_boost": 0.78,
        "style": 0.45,
        "use_speaker_boost": True,
    }
    base_profile: dict[str, object] = {
        "engine": "elevenlabs",
        "model": "eleven_multilingual_v2",
        "voice_settings": base_voice_settings,
    }
    comparison_profile = {
        **base_profile,
        "voice_settings": {**base_voice_settings, "stability": 0.6},
    }
    voice = "RXoaSpLaWTEckJgPUBG3"
    results = [
        audition.VoiceAuditionResult(
            provider="elevenlabs",
            voice=voice,
            label="ad-Dottoressa-Tiziana-stab42",
            source="configured",
            used_by=("ad:Dottoressa Tiziana",),
            status="generated",
            profile=base_profile,
            text_sha256="a" * 64,
            audio_sha256="b" * 64,
            audio_duration_seconds=7.25,
        ),
        audition.VoiceAuditionResult(
            provider="elevenlabs",
            voice=voice,
            label="ad-Dottoressa-Tiziana-stab60",
            source="configured",
            used_by=("ad:Dottoressa Tiziana",),
            status="generated",
            profile=comparison_profile,
            text_sha256="c" * 64,
            audio_sha256="d" * 64,
            audio_duration_seconds=7.5,
        ),
    ]
    audition.write_manifest(results, tmp_path, config_path=tmp_path / "radio.toml", timestamp="20260713T120000Z")
    manifest = json.loads(manifest_path.read_text())
    candidate_ids = [result["candidate_id"] for result in manifest["results"]]
    assert candidate_ids[0] != candidate_ids[1]
    decisions_path.write_text(
        json.dumps(
            [
                {
                    "candidate_id": candidate_ids[0],
                    "candidate_name": "Dottoressa Tiziana",
                    "approval_status": "accepted",
                    "rationale": "accepted_balanced_brand_fit",
                },
                {
                    "candidate_id": candidate_ids[1],
                    "candidate_name": "Dottoressa Tiziana",
                    "approval_status": "rejected",
                    "rationale": "rejected_profile_mismatch",
                },
            ]
        )
    )

    audition.write_selection_receipt_from_manifest(
        manifest_path=manifest_path,
        decisions_path=decisions_path,
        path=receipt_path,
    )

    payload = audition.load_selection_receipt(receipt_path)
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    receipt_candidate_ids: list[object] = []
    receipt_approval_statuses: list[object] = []
    for candidate in candidates:
        assert isinstance(candidate, dict)
        receipt_candidate_ids.append(candidate["candidate_id"])
        receipt_approval_statuses.append(candidate["approval_status"])
    assert receipt_candidate_ids == candidate_ids
    assert receipt_approval_statuses == ["accepted", "rejected"]


def test_selection_receipt_writer_does_not_clobber_a_competing_receipt(tmp_path, monkeypatch) -> None:
    receipt_path = tmp_path / "proof" / "selection.json"

    def competing_link(_source: Path, destination: Path) -> None:
        destination.write_text("reviewed receipt\n", encoding="utf-8")
        raise FileExistsError(destination)

    monkeypatch.setattr(audition.os, "link", competing_link)

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        audition.write_selection_receipt([_selection_candidate()], path=receipt_path)

    assert receipt_path.read_text(encoding="utf-8") == "reviewed receipt\n"
    assert not list(receipt_path.parent.glob("*.tmp"))


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


def _host_performance_entry(
    *,
    host: str = "Marco",
    voice_id: str = "o4b57JYAECRMJyCEXyIE",
    model: str = audition.ELEVENLABS_V3_MODEL,
    delivery_profile: str = "marco",
    delivery_cue: str = "neutral",
    human_disposition: str = "accepted",
    rationale: str = "accepted_v3_tonal_fit",
) -> dict[str, object]:
    clean_text_sha256 = "a" * 64
    rendered_text_sha256 = (
        clean_text_sha256
        if delivery_cue == audition.NEUTRAL_DELIVERY_CUE
        else hashlib.sha256(f"{delivery_profile}:{delivery_cue}".encode()).hexdigest()
    )
    return {
        "performance_id": audition._host_performance_id(
            "elevenlabs",
            voice_id,
            model,
            delivery_profile,
            delivery_cue,
            clean_text_sha256,
            rendered_text_sha256,
        ),
        "host": host,
        "voice_id": voice_id,
        "model": model,
        "delivery_profile": delivery_profile,
        "delivery_cue": delivery_cue,
        "clean_text_sha256": clean_text_sha256,
        "rendered_text_sha256": rendered_text_sha256,
        "provider_result": audition.STATUS_GENERATED,
        "audio_sha256": "b" * 64,
        "audio_duration_seconds": 7.25,
        "human_disposition": human_disposition,
        "rationale": rationale,
    }


def _approved_host_performance_matrix() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for host, profile, voice_id in (
        ("Marco", "marco", "o4b57JYAECRMJyCEXyIE"),
        ("Giulia", "giulia", "fNmw8sukfGuvWVOp33Ge"),
    ):
        rows.append(
            _host_performance_entry(
                host=host,
                voice_id=voice_id,
                model=audition.ELEVENLABS_V2_MODEL,
                delivery_profile=profile,
            )
        )
        rows.append(
            _host_performance_entry(
                host=host,
                voice_id=voice_id,
                model=audition.ELEVENLABS_V3_MODEL,
                delivery_profile=profile,
            )
        )
        for cue in audition.V3_DELIVERY_CUES_BY_PROFILE[profile]:
            rows.append(
                _host_performance_entry(
                    host=host,
                    voice_id=voice_id,
                    model=audition.ELEVENLABS_V3_MODEL,
                    delivery_profile=profile,
                    delivery_cue=cue,
                )
            )
    return rows


def _v3_host_config() -> StationConfig:
    config = _station_config()
    marco, giulia = config.hosts
    marco.engine = "elevenlabs"
    marco.voice = "o4b57JYAECRMJyCEXyIE"
    marco.voice_settings = {"stability": 0.6}
    marco.elevenlabs_model = audition.ELEVENLABS_V3_MODEL
    marco.delivery_profile = "marco"
    giulia.engine = "elevenlabs"
    giulia.voice = "fNmw8sukfGuvWVOp33Ge"
    giulia.elevenlabs_model = audition.ELEVENLABS_V3_MODEL
    giulia.delivery_profile = "giulia"
    return config


def test_build_v3_host_performance_targets_pairs_v2_v3_and_profile_cues() -> None:
    targets = audition.build_v3_host_performance_targets(_v3_host_config())

    by_label = {target.label: target for target in targets}
    assert set(by_label) == {
        "host-Marco-v2-clean",
        "host-Marco-v3-clean",
        "host-Marco-v3-energetic",
        "host-Marco-v3-curious",
        "host-Marco-v3-playful",
        "host-Giulia-v2-clean",
        "host-Giulia-v3-clean",
        "host-Giulia-v3-dry",
        "host-Giulia-v3-curious",
        "host-Giulia-v3-playful",
    }
    assert by_label["host-Marco-v2-clean"].elevenlabs_model == audition.ELEVENLABS_V2_MODEL
    assert by_label["host-Marco-v2-clean"].delivery_cue == audition.NEUTRAL_DELIVERY_CUE
    assert by_label["host-Marco-v3-energetic"].elevenlabs_model == audition.ELEVENLABS_V3_MODEL
    assert by_label["host-Marco-v3-energetic"].voice_settings == {"stability": 0.6}
    assert by_label["host-Giulia-v3-dry"].delivery_profile == "giulia"
    assert len({target.text for target in targets if "host:Marco" in target.used_by}) == 1
    assert len({target.text for target in targets if "host:Giulia" in target.used_by}) == 1


@pytest.mark.asyncio
async def test_synthesize_v3_target_threads_model_profile_and_semantic_cue(tmp_path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_elevenlabs(text, voice, output_path, **kwargs):
        calls.append({"text": text, "voice": voice, **kwargs})
        output_path.write_bytes(b"v3 mp3")
        return output_path

    monkeypatch.setattr(audition.tts_module, "synthesize_elevenlabs", fake_elevenlabs)
    target = audition.VoiceAuditionTarget(
        provider="elevenlabs",
        voice="o4b57JYAECRMJyCEXyIE",
        label="host-Marco-v3-energetic",
        source="v3-host-performance",
        text="Una prova pulita.",
        voice_settings={"stability": 0.6},
        elevenlabs_model=audition.ELEVENLABS_V3_MODEL,
        delivery_profile="marco",
        delivery_cue="energetic",
    )

    await audition._synthesize_target(target, tmp_path / "marco.mp3")

    assert calls == [
        {
            "text": "Una prova pulita.",
            "voice": "o4b57JYAECRMJyCEXyIE",
            "voice_settings": {"stability": 0.6},
            "elevenlabs_model": "eleven_v3",
            "delivery_cue": "energetic",
            "delivery_profile": "marco",
        }
    ]


@pytest.mark.asyncio
async def test_v3_audition_manifest_hashes_clean_and_provider_rendered_text_separately(tmp_path, monkeypatch) -> None:
    async def fake_synthesize(target: audition.VoiceAuditionTarget, output_path: Path) -> Path:
        output_path.write_bytes(b"v3-rendered")
        return output_path

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    monkeypatch.setattr(audition, "probe_duration_sec", lambda _path: 6.5)
    target = audition.VoiceAuditionTarget(
        provider="elevenlabs",
        voice="o4b57JYAECRMJyCEXyIE",
        label="host-Marco-v3-energetic",
        source="v3-host-performance",
        text="Una prova pulita.",
        elevenlabs_model=audition.ELEVENLABS_V3_MODEL,
        delivery_profile="marco",
        delivery_cue="energetic",
    )

    [result] = await audition.run_auditions([target], tmp_path, env={"ELEVENLABS_API_KEY": "test"})

    assert result.text_sha256 == hashlib.sha256(target.text.encode()).hexdigest()
    assert result.clean_text_sha256 == result.text_sha256
    assert result.rendered_text_sha256 == hashlib.sha256(b"[excited] Una prova pulita.").hexdigest()
    assert result.clean_text_sha256 != result.rendered_text_sha256
    assert result.audio_sha256 == hashlib.sha256(b"v3-rendered").hexdigest()


def test_host_performance_receipt_keeps_only_complete_safe_evidence_and_gate_matrix(tmp_path) -> None:
    receipt_path = tmp_path / "proof" / "v3-host-performance.json"

    written = audition.write_host_performance_receipt(_approved_host_performance_matrix(), path=receipt_path)

    assert written == receipt_path
    payload = audition.load_host_performance_receipt(receipt_path, require_approved_matrix=True)
    assert set(payload) == {"schema_version", "performances"}
    assert "La prossima canzone" not in receipt_path.read_text()
    assert "audio_path" not in receipt_path.read_text()
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        audition.write_host_performance_receipt(_approved_host_performance_matrix(), path=receipt_path)


@pytest.mark.parametrize(
    ("extra_field", "value"),
    [
        ("text", "Questa copia non appartiene al receipt"),
        ("rendered_text", "[excited] Questa copia non appartiene al receipt"),
        ("audio_path", "/tmp/voice-auditions/marco.mp3"),
        ("credentials", "elevenlabs-secret"),
    ],
)
def test_host_performance_receipt_rejects_raw_copy_paths_and_credentials(extra_field: str, value: str) -> None:
    entry = _host_performance_entry()
    entry[extra_field] = value

    with pytest.raises(ValueError, match="prohibited fields"):
        audition.host_performance_receipt([entry])


def test_host_performance_gate_blocks_rejected_or_incomplete_v3_matrix() -> None:
    rows = _approved_host_performance_matrix()
    rejected = rows[-1]
    rejected["human_disposition"] = "rejected"
    rejected["rationale"] = "rejected_audio_artifacts"

    receipt = audition.host_performance_receipt(rows)

    with pytest.raises(ValueError, match="is not approved"):
        audition.assert_host_performance_gate(receipt)


def test_host_performance_gate_blocks_incomplete_v3_matrix() -> None:
    """A missing required (model, cue) row must fail the gate even when every present
    row is generated and accepted — an incomplete comparison is not a passing gate."""
    rows = _approved_host_performance_matrix()
    # Drop Marco's eleven_v3/energetic row; the rest of the matrix is fully approved.
    incomplete = [
        row
        for row in rows
        if not (
            row["delivery_profile"] == "marco"
            and row["model"] == audition.ELEVENLABS_V3_MODEL
            and row["delivery_cue"] == "energetic"
        )
    ]
    assert len(incomplete) == len(rows) - 1

    receipt = audition.host_performance_receipt(incomplete)

    with pytest.raises(ValueError, match="is missing marco rows"):
        audition.assert_host_performance_gate(receipt)


def test_host_performance_receipt_from_manifest_redacts_local_paths_and_requires_match(tmp_path) -> None:
    target = audition.VoiceAuditionTarget(
        provider="elevenlabs",
        voice="o4b57JYAECRMJyCEXyIE",
        label="host-Marco-v3-clean",
        source="v3-host-performance",
        used_by=("host:Marco", "v3_performance:marco"),
        text="Una prova pulita.",
        elevenlabs_model=audition.ELEVENLABS_V3_MODEL,
        delivery_profile="marco",
        delivery_cue="neutral",
    )
    result = audition._result_for_target(
        target,
        status=audition.STATUS_GENERATED,
        output_path="/tmp/voice-auditions/marco.mp3",
        audio_sha256="b" * 64,
        audio_duration_seconds=7.25,
    )
    manifest_path = audition.write_manifest(
        [result], tmp_path, config_path=tmp_path / "radio.toml", timestamp="20260716T120000Z"
    )
    manifest = json.loads(manifest_path.read_text())
    performance_id = manifest["results"][0]["performance_id"]
    receipt = audition.host_performance_receipt_from_manifest(
        manifest,
        [
            {
                "performance_id": performance_id,
                "host": "Marco",
                "human_disposition": "accepted",
                "rationale": "accepted_v3_tonal_fit",
            }
        ],
    )

    performances = receipt["performances"]
    assert isinstance(performances, list)
    first_performance = performances[0]
    assert isinstance(first_performance, dict)
    assert first_performance["performance_id"] == performance_id
    assert "/tmp/voice-auditions" not in json.dumps(receipt)


def test_cli_dry_run_lists_v3_host_performance_matrix_without_writing_files(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(audition, "load_config", lambda _path: _v3_host_config())
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")

    rc = audition.main(
        [
            "--providers",
            "elevenlabs",
            "--v3-host-performance",
            "--dry-run",
            "--output-dir",
            str(tmp_path),
            "--timestamp",
            "20260716T120000Z",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "planned\televenlabs\to4b57JYAECRMJyCEXyIE" in captured.out
    assert "model=eleven_v3 profile=marco cue=energetic" in captured.out
    assert "model=eleven_v3 profile=giulia cue=dry" in captured.out
    assert list(tmp_path.iterdir()) == []


def test_cli_verifies_complete_host_performance_gate(tmp_path, capsys) -> None:
    receipt_path = tmp_path / "proof" / "v3-host-performance.json"
    audition.write_host_performance_receipt(_approved_host_performance_matrix(), path=receipt_path)

    rc = audition.main(
        [
            "--host-performance-receipt-path",
            str(receipt_path),
            "--verify-host-performance-gate",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "Host-performance gate: approved" in captured.out


def test_committed_host_performance_receipt_is_valid_when_human_approval_adds_it() -> None:
    """A committed receipt is an immutable record of a real audition, so it must
    always load and validate. It also can never disagree with production config:
    V3 may ship only when the receipt approves the full Marco/Giulia matrix, so if
    the gate does not pass (e.g. V3 was auditioned and rejected) no host may be
    configured on eleven_v3."""
    # The receipt is a durable, tracked audit trail — it must stay committed so
    # this cross-artifact guard can never be silently disabled by deleting it.
    assert audition.HOST_PERFORMANCE_RECEIPT_PATH.exists(), (
        f"committed host-performance receipt is missing: {audition.HOST_PERFORMANCE_RECEIPT_PATH}"
    )

    from mammamiradio.core.config import load_config

    # Always a valid immutable record (raises on a malformed or tampered receipt).
    receipt = audition.load_host_performance_receipt()

    try:
        audition.assert_host_performance_gate(receipt)
        gate_approves_v3 = True
    except ValueError as exc:
        # Only the human-rejection outcome is an acceptable non-approval. A
        # structural failure (missing/duplicate rows, inconsistent hashes) must
        # fail loudly rather than pass as "V3 simply not approved".
        assert str(exc).startswith("host performance receipt is not approved for "), (
            f"unexpected host-performance gate failure: {exc}"
        )
        gate_approves_v3 = False

    config = load_config(str(Path(__file__).resolve().parents[2] / "radio.toml"))
    hosts_on_v3 = [h.name for h in config.hosts if h.elevenlabs_model == "eleven_v3"]

    if not gate_approves_v3:
        assert hosts_on_v3 == [], (
            "radio.toml ships hosts on eleven_v3 but the committed host-performance "
            f"receipt does not approve V3: {hosts_on_v3}"
        )

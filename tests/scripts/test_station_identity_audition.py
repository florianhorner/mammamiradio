from __future__ import annotations

import hashlib
import json
import subprocess
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
from mammamiradio.core.models import HostPersonality
from scripts import audition_tts_voices as audition


def _identity_config() -> StationConfig:
    return StationConfig(
        station=StationSection(),
        playlist=PlaylistSection(),
        pacing=PacingSection(),
        hosts=[
            HostPersonality(
                name="Marco",
                voice="o4b57JYAECRMJyCEXyIE",
                style="manic",
                engine="elevenlabs",
                edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
                voice_settings={"stability": 0.6},
                elevenlabs_model=audition.ELEVENLABS_V2_MODEL,
            ),
            HostPersonality(
                name="Giulia",
                voice="fNmw8sukfGuvWVOp33Ge",
                style="dry",
                engine="elevenlabs",
                edge_fallback_voice="it-IT-IsabellaNeural",
                elevenlabs_model=audition.ELEVENLABS_V2_MODEL,
            ),
        ],
        ads=AdsSection(),
        sonic_brand=SonicBrandSection(
            full_ident=audition.IDENTITY_ANNOUNCER_TEXT,
            sweeper_voice="it-IT-Isabella:DragonHDLatestNeural",
            sweeper_engine="azure",
            sweeper_edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
        ),
        super_italian_mode=False,
    )


def _enable_provider_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SPEECH_KEY", "test-azure-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "test-region")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-elevenlabs-key")


def _patch_identity_config(monkeypatch: pytest.MonkeyPatch, config: StationConfig | None = None) -> StationConfig:
    resolved = config or _identity_config()
    monkeypatch.setattr(audition, "load_config", lambda _path: resolved)
    return resolved


def _patch_audio_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_audio_evidence(path: Path, config: StationConfig) -> tuple[str, float, dict[str, object]]:
        return (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            1.75,
            {
                "codec": "mp3",
                "sample_rate_hz": config.audio.sample_rate,
                "channels": config.audio.channels,
                "bitrate_kbps": config.audio.bitrate,
            },
        )

    monkeypatch.setattr(audition, "_identity_audio_evidence", fake_audio_evidence)


async def _render_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stamp: str) -> tuple[Path, dict]:
    _enable_provider_credentials(monkeypatch)
    _patch_identity_config(monkeypatch)
    _patch_audio_evidence(monkeypatch)

    async def fake_synthesize(target: audition.VoiceAuditionTarget, output_path: Path) -> Path:
        output_path.write_bytes(f"audio:{target.label}".encode())
        return output_path

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    return await audition.render_identity_recalibration(
        tmp_path / "boards",
        config_path=audition.DEFAULT_CONFIG_PATH,
        timestamp=stamp,
    )


def _decision_path(
    tmp_path: Path,
    pack_digest: str,
    *,
    status: str = "approved",
    wrong: str = "none",
    rationale: str = "accepted_station_identity",
    extra: dict[str, object] | None = None,
) -> Path:
    value: dict[str, object] = {
        "pack_digest": pack_digest,
        "status": status,
        "wrong": wrong,
        "rationale": rationale,
    }
    value.update(extra or {})
    path = tmp_path / "identity-decision.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_configured_host_targets_keep_production_voice_settings() -> None:
    targets = audition.collect_configured_targets(_identity_config())
    marco = next(target for target in targets if target.used_by == ("host:Marco",))

    assert marco.provider == "elevenlabs"
    assert marco.voice_settings == {"stability": 0.6}


def test_identity_targets_match_production_routes_and_normal_mode_copy() -> None:
    targets = audition.build_identity_recalibration_targets(_identity_config())

    assert [(target.label, target.provider, target.voice) for target in targets] == [
        ("identity-isabella", "azure", "it-IT-Isabella:DragonHDLatestNeural"),
        ("identity-marco", "elevenlabs", "o4b57JYAECRMJyCEXyIE"),
        ("identity-giulia", "elevenlabs", "fNmw8sukfGuvWVOp33Ge"),
    ]
    assert targets[0].text == audition.IDENTITY_ANNOUNCER_TEXT
    assert targets[1].text == "And now... a word from our sponsors, amici!"
    assert targets[2].text == "Back to the music, finally — grazie for staying with us!"
    assert targets[1].voice_settings == {"stability": 0.6}
    assert targets[2].voice_settings is None


@pytest.mark.parametrize(
    "scenario",
    ["announcer-edge", "marco-openai", "giulia-v3", "missing-marco", "super-italian"],
)
def test_identity_targets_reject_noncanonical_route_before_synthesis(scenario: str) -> None:
    config = _identity_config()
    if scenario == "announcer-edge":
        config.sonic_brand.sweeper_engine = "edge"
    elif scenario == "marco-openai":
        config.hosts[0].engine = "openai"
    elif scenario == "giulia-v3":
        config.hosts[1].elevenlabs_model = audition.ELEVENLABS_V3_MODEL
    elif scenario == "missing-marco":
        config.hosts = [host for host in config.hosts if host.name != "Marco"]
    else:
        config.super_italian_mode = True

    with pytest.raises(ValueError):
        audition.build_identity_recalibration_targets(config)


@pytest.mark.asyncio
async def test_identity_board_records_verified_routes_hashes_and_three_players(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamp = "20260815T180000Z"
    run_dir, manifest = await _render_board(tmp_path, monkeypatch, stamp=stamp)

    assert run_dir == tmp_path / "boards" / f"identity-gate-{stamp}"
    assert manifest["schema_version"] == 2
    assert manifest["stage"] == "voice-identity-recalibration"
    assert manifest["active_spoken_mode"] == "normal"
    assert manifest["release_ready"] is False
    assert (run_dir / ".ready").read_text().strip() == manifest["pack_digest"]
    listening_receipt = manifest["listening_receipt"]
    assert listening_receipt["status"] == "pending"
    assert listening_receipt["pack_digest"] == manifest["pack_digest"]
    clips = manifest["clips"]
    assert len(clips) == 3
    by_character = {clip["character"]: clip for clip in clips}
    assert by_character["Marco"]["copy_source"].endswith("AD_BREAK_NORMAL_INTROS")
    assert by_character["Giulia"]["copy_source"].endswith("AD_BREAK_NORMAL_OUTROS")
    assert by_character["Marco"]["route"]["requested"]["settings"] == {
        "similarity_boost": 0.78,
        "stability": 0.6,
        "style": 0.45,
        "use_speaker_boost": True,
    }
    assert by_character["Giulia"]["route"]["requested"]["settings"]["stability"] == 0.42
    for clip in clips:
        assert clip["format"] == {
            "codec": "mp3",
            "sample_rate_hz": 48_000,
            "channels": 2,
            "bitrate_kbps": 192,
        }
        assert clip["route"]["requested"] == clip["route"]["effective"]
        assert clip["route"]["fallback_used"] is False
        assert (run_dir / clip["path"]).is_file()

    html_text = (run_dir / "index.html").read_text(encoding="utf-8")
    assert html_text.count("<audio ") == 3
    assert "Alice" not in html_text
    assert "no sound treatment" in html_text.lower()
    stored, paths = audition._validate_identity_board(run_dir / "manifest.json")
    assert stored["pack_digest"] == manifest["pack_digest"]
    assert len(paths) == 3
    with pytest.raises(ValueError, match="not approved"):
        audition.assert_identity_listening_gate(run_dir / "manifest.json")


@pytest.mark.asyncio
async def test_identity_board_missing_credentials_calls_no_provider_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_identity_config(monkeypatch)
    for name in ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    calls = 0

    async def fake_synthesize(_target: audition.VoiceAuditionTarget, _output_path: Path) -> Path:
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    output_root = tmp_path / "boards"

    with pytest.raises(ValueError, match="missing provider credentials"):
        await audition.render_identity_recalibration(
            output_root,
            config_path=audition.DEFAULT_CONFIG_PATH,
            timestamp="20260815T180001Z",
        )

    assert calls == 0
    assert not output_root.exists()


@pytest.mark.asyncio
async def test_identity_provider_failure_stops_before_later_paid_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_provider_credentials(monkeypatch)
    _patch_identity_config(monkeypatch)
    _patch_audio_evidence(monkeypatch)
    calls: list[str] = []

    async def fake_synthesize(target: audition.VoiceAuditionTarget, output_path: Path) -> Path:
        calls.append(target.label)
        if target.label == "identity-marco":
            raise RuntimeError("provider rejected render")
        output_path.write_bytes(f"audio:{target.label}".encode())
        return output_path

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    output_root = tmp_path / "boards"

    with pytest.raises(RuntimeError, match="failed closed at identity-marco"):
        await audition.render_identity_recalibration(
            output_root,
            config_path=audition.DEFAULT_CONFIG_PATH,
            timestamp="20260815T180002Z",
        )

    assert calls == ["identity-isabella", "identity-marco"]
    assert output_root.is_dir()
    assert list(output_root.iterdir()) == []


@pytest.mark.asyncio
async def test_identity_invalid_audio_stops_before_later_paid_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_provider_credentials(monkeypatch)
    _patch_identity_config(monkeypatch)
    calls: list[str] = []

    async def fake_synthesize(target: audition.VoiceAuditionTarget, output_path: Path) -> Path:
        calls.append(target.label)
        output_path.write_bytes(b"invalid audio")
        return output_path

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    monkeypatch.setattr(
        audition,
        "_identity_audio_evidence",
        lambda _path, _config: (_ for _ in ()).throw(RuntimeError("wrong format")),
    )

    with pytest.raises(RuntimeError, match="failed closed at identity-isabella"):
        await audition.render_identity_recalibration(
            tmp_path / "boards",
            config_path=audition.DEFAULT_CONFIG_PATH,
            timestamp="20260815T180003Z",
        )

    assert calls == ["identity-isabella"]
    assert list((tmp_path / "boards").iterdir()) == []


@pytest.mark.asyncio
async def test_identity_board_claim_refuses_overwrite_before_synthesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_provider_credentials(monkeypatch)
    _patch_identity_config(monkeypatch)
    calls = 0

    async def fake_synthesize(_target: audition.VoiceAuditionTarget, output_path: Path) -> Path:
        nonlocal calls
        calls += 1
        output_path.write_bytes(b"audio")
        return output_path

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    stamp = "20260815T180004Z"
    output_root = tmp_path / "boards"
    existing = output_root / f"identity-gate-{stamp}"
    existing.mkdir(parents=True)
    sentinel = existing / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        await audition.render_identity_recalibration(
            output_root,
            config_path=audition.DEFAULT_CONFIG_PATH,
            timestamp=stamp,
        )

    assert calls == 0
    assert sentinel.read_text() == "keep"


@pytest.mark.asyncio
async def test_identity_staging_failure_releases_claimed_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_provider_credentials(monkeypatch)
    _patch_identity_config(monkeypatch)
    monkeypatch.setattr(
        audition.tempfile,
        "mkdtemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    output_root = tmp_path / "boards"

    with pytest.raises(OSError, match="disk unavailable"):
        await audition.render_identity_recalibration(
            output_root,
            config_path=audition.DEFAULT_CONFIG_PATH,
            timestamp="20260815T180009Z",
        )

    assert output_root.is_dir()
    assert list(output_root.iterdir()) == []


@pytest.mark.asyncio
async def test_identity_renderer_rejects_bad_timestamp_and_noncanonical_config_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_synthesize(_target: audition.VoiceAuditionTarget, _output_path: Path) -> Path:
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    with pytest.raises(ValueError, match="timestamp"):
        await audition.render_identity_recalibration(
            tmp_path / "boards",
            config_path=audition.DEFAULT_CONFIG_PATH,
            timestamp="../bad",
        )
    alternate_config = tmp_path / "radio.toml"
    alternate_config.write_text("super_italian_mode = false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical repository config"):
        await audition.render_identity_recalibration(
            tmp_path / "boards",
            config_path=alternate_config,
            timestamp="20260815T180010Z",
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_identity_renderer_rejects_helper_output_path_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_provider_credentials(monkeypatch)
    _patch_identity_config(monkeypatch)
    calls: list[str] = []

    async def fake_synthesize(target: audition.VoiceAuditionTarget, output_path: Path) -> Path:
        calls.append(target.label)
        wrong_path = output_path.with_name("wrong.mp3")
        wrong_path.write_bytes(b"audio")
        return wrong_path

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    with pytest.raises(RuntimeError, match="unexpected output path"):
        await audition.render_identity_recalibration(
            tmp_path / "boards",
            config_path=audition.DEFAULT_CONFIG_PATH,
            timestamp="20260815T180011Z",
        )
    assert calls == ["identity-isabella"]
    assert list((tmp_path / "boards").iterdir()) == []


def test_identity_audio_probe_requires_exact_configured_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = Path("-clip.mp3")
    path.write_bytes(b"audio")
    sample_rate = "48000"
    commands: list[list[str]] = []

    def fake_ffprobe(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        payload = {
            "streams": [
                {
                    "codec_name": "mp3",
                    "sample_rate": sample_rate,
                    "channels": 2,
                    "bit_rate": "192000",
                }
            ],
            "format": {"duration": "1.234", "bit_rate": "192000"},
        }
        return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

    monkeypatch.setattr(
        audition.subprocess,
        "run",
        fake_ffprobe,
    )

    audio_format, duration = audition._probe_identity_audio(path, _identity_config())

    assert audio_format["sample_rate_hz"] == 48_000
    assert audio_format["channels"] == 2
    assert audio_format["bitrate_kbps"] == 192
    assert duration == 1.234
    assert commands[0][-1] == str(path.resolve())

    sample_rate = "44100"
    with pytest.raises(RuntimeError, match="expected"):
        audition._probe_identity_audio(path, _identity_config())


@pytest.mark.asyncio
async def test_identity_receipt_approves_only_exact_current_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = await _render_board(tmp_path, monkeypatch, stamp="20260815T180005Z")
    manifest_path = run_dir / "manifest.json"
    decision_path = _decision_path(tmp_path, manifest["pack_digest"])

    audition.write_identity_listening_receipt_from_decision(
        manifest_path=manifest_path,
        decision_path=decision_path,
        reviewed_at="2026-08-15T20:00:00+02:00",
    )

    assert len(audition.assert_identity_listening_gate(manifest_path)) == 3
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["listening_receipt"] == {
        "schema_version": 1,
        "status": "approved",
        "pack_digest": manifest["pack_digest"],
        "target": "Mac",
        "prompt": audition.IDENTITY_LISTENING_PROMPT,
        "wrong": "none",
        "rationale": "accepted_station_identity",
        "reviewed_at": "2026-08-15T20:00:00+02:00",
    }


@pytest.mark.asyncio
async def test_approved_identity_ignores_ad_only_config_drift_but_rejects_voice_settings_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = (tmp_path / "radio.toml").resolve()
    config_path.write_text('[[ads.brands]]\nname = "Test"\nsonic_recipe = "old-recipe"\n', encoding="utf-8")
    monkeypatch.setattr(audition, "DEFAULT_CONFIG_PATH", config_path)
    config = _identity_config()
    _enable_provider_credentials(monkeypatch)
    _patch_identity_config(monkeypatch, config)
    _patch_audio_evidence(monkeypatch)

    async def fake_synthesize(target: audition.VoiceAuditionTarget, output_path: Path) -> Path:
        output_path.write_bytes(f"audio:{target.label}".encode())
        return output_path

    monkeypatch.setattr(audition, "_synthesize_target", fake_synthesize)
    run_dir, manifest = await audition.render_identity_recalibration(
        tmp_path / "boards",
        config_path=config_path,
        timestamp="20260815T180017Z",
    )
    manifest_path = run_dir / "manifest.json"
    audition.write_identity_listening_receipt_from_decision(
        manifest_path=manifest_path,
        decision_path=_decision_path(tmp_path, str(manifest["pack_digest"])),
        reviewed_at="2026-08-15T20:00:00+02:00",
    )

    config_path.write_text('[[ads.brands]]\nname = "Test"\nsonic_recipe = "new-recipe"\n', encoding="utf-8")

    assert len(audition.assert_identity_listening_gate(manifest_path)) == 3
    assert (
        json.loads(manifest_path.read_text(encoding="utf-8"))["config"]["sha256"]
        != hashlib.sha256(config_path.read_bytes()).hexdigest()
    )

    config.hosts[0].voice_settings = {"stability": 0.5}
    with pytest.raises(ValueError, match="documented production voice profile"):
        audition.assert_identity_listening_gate(manifest_path)

    config.hosts[0].voice_settings = {"stability": 0.6}
    config.hosts[0].engine = "openai"
    with pytest.raises(ValueError, match="must use ElevenLabs"):
        audition.assert_identity_listening_gate(manifest_path)


def test_identity_source_evidence_keeps_live_hash_checks_strict_by_default(tmp_path: Path) -> None:
    source_path = (tmp_path / "casting-proof.json").resolve()
    source_path.write_bytes(b"approved")
    approved_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    evidence = {"path": str(source_path), "sha256": approved_digest}
    source_path.write_bytes(b"changed")

    with pytest.raises(ValueError, match="current file hash differs"):
        audition._identity_source_evidence(evidence, "host_profile_provenance")

    assert audition._identity_source_evidence(
        evidence,
        "config",
        require_current_hash=False,
    ) == (source_path, approved_digest)


@pytest.mark.asyncio
async def test_identity_receipt_bad_or_second_decision_cannot_mutate_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = await _render_board(tmp_path, monkeypatch, stamp="20260815T180006Z")
    manifest_path = run_dir / "manifest.json"
    before = manifest_path.read_bytes()
    bad_decision = _decision_path(tmp_path, manifest["pack_digest"], extra={"notes": "secret"})

    with pytest.raises(ValueError, match="invalid field set"):
        audition.write_identity_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=bad_decision,
        )
    assert manifest_path.read_bytes() == before

    approved = _decision_path(tmp_path, manifest["pack_digest"])
    audition.write_identity_listening_receipt_from_decision(
        manifest_path=manifest_path,
        decision_path=approved,
        reviewed_at="2026-08-15T20:00:00+02:00",
    )
    after = manifest_path.read_bytes()
    with pytest.raises(ValueError, match="already been decided"):
        audition.write_identity_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=approved,
        )
    assert manifest_path.read_bytes() == after


@pytest.mark.asyncio
async def test_identity_receipt_lock_prevents_concurrent_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = await _render_board(tmp_path, monkeypatch, stamp="20260815T180012Z")
    manifest_path = run_dir / "manifest.json"
    before = manifest_path.read_bytes()
    (run_dir / ".listening-receipt.lock").write_text("held", encoding="utf-8")

    with pytest.raises(ValueError, match="already being decided"):
        audition.write_identity_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=_decision_path(tmp_path, manifest["pack_digest"]),
        )

    assert manifest_path.read_bytes() == before


@pytest.mark.asyncio
async def test_identity_gate_binds_listening_surface_and_exact_manifest_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = await _render_board(tmp_path, monkeypatch, stamp="20260815T180013Z")
    manifest_path = run_dir / "manifest.json"
    index_path = run_dir / "index.html"
    first_path = manifest["clips"][0]["path"]
    second_path = manifest["clips"][1]["path"]
    index_path.write_text(index_path.read_text().replace(first_path, second_path), encoding="utf-8")

    with pytest.raises(ValueError, match="listening surface differs"):
        audition._validate_identity_board(manifest_path)

    index_path.write_text(audition._identity_html(manifest), encoding="utf-8")
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored["api_key"] = "must-not-survive"
    manifest_path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid field set"):
        audition._validate_identity_board(manifest_path)


@pytest.mark.asyncio
async def test_identity_gate_rejects_rejection_and_stale_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = await _render_board(tmp_path, monkeypatch, stamp="20260815T180007Z")
    manifest_path = run_dir / "manifest.json"
    rejected = _decision_path(
        tmp_path,
        manifest["pack_digest"],
        status="rejected",
        wrong="Marco",
        rationale="rejected_wrong_voice_identity",
    )
    audition.write_identity_listening_receipt_from_decision(
        manifest_path=manifest_path,
        decision_path=rejected,
        reviewed_at="2026-08-15T20:00:00+02:00",
    )
    with pytest.raises(ValueError, match="not approved"):
        audition.assert_identity_listening_gate(manifest_path)

    first_clip = manifest["clips"][0]
    (run_dir / first_clip["path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="audio hash differs"):
        audition._validate_identity_board(manifest_path)


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--strict"],
        ["--dry-run"],
        ["--elevenlabs-stability"],
        ["--elevenlabs-stability", "0.6"],
        ["--voice", "elevenlabs:voice"],
        ["--sample-text", "different"],
        ["--providers", "elevenlabs"],
        ["--overwrite-selection-receipt"],
        ["--overwrite-host-performance-receipt"],
    ],
)
def test_identity_cli_rejects_ignored_or_conflicting_flags_before_config_load(
    extra_args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        audition,
        "load_config",
        lambda _path: (_ for _ in ()).throw(AssertionError("config must not load")),
    )

    assert audition.main(["--identity-recalibration", *extra_args]) == 2


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--strict"],
        ["--dry-run"],
        ["--elevenlabs-stability"],
        ["--providers", "azure"],
        ["--timestamp", "20260815T180014Z"],
        ["--output-dir", "/tmp/ignored-output"],
    ],
)
def test_identity_receipt_cli_rejects_unrelated_audition_options(
    extra_args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        audition,
        "assert_identity_listening_gate",
        lambda _path: (_ for _ in ()).throw(AssertionError("gate must not run")),
    )

    result = audition.main(
        [
            "--identity-listening-manifest",
            "/tmp/manifest.json",
            "--verify-identity-listening-gate",
            *extra_args,
        ]
    )

    assert result == 2


def test_identity_cli_passes_only_bound_paths_to_renderer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    run_dir = tmp_path / "result"

    async def fake_render(output_root: Path, **kwargs) -> tuple[Path, dict[str, object]]:
        captured["output_root"] = output_root
        captured.update(kwargs)
        return run_dir, {"pack_digest": "a" * 64}

    monkeypatch.setattr(audition, "render_identity_recalibration", fake_render)
    config_path = tmp_path / "radio.toml"
    receipt_path = tmp_path / "host-receipt.json"
    output_root = tmp_path / "boards"

    result = audition.main(
        [
            "--identity-recalibration",
            "--config",
            str(config_path),
            "--output-dir",
            str(output_root),
            "--timestamp",
            "20260815T180008Z",
            "--host-performance-receipt-path",
            str(receipt_path),
        ]
    )

    assert result == 0
    assert captured == {
        "output_root": output_root,
        "config_path": config_path,
        "timestamp": "20260815T180008Z",
        "receipt_path": receipt_path,
    }


@pytest.mark.asyncio
async def test_approved_identity_clips_are_keyed_by_id_with_exact_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, manifest = await _render_board(tmp_path, monkeypatch, stamp="20260815T180015Z")
    manifest_path = run_dir / "manifest.json"
    audition.write_identity_listening_receipt_from_decision(
        manifest_path=manifest_path,
        decision_path=_decision_path(tmp_path, manifest["pack_digest"]),
        reviewed_at="2026-08-15T20:00:00+02:00",
    )

    pack_digest, paths_by_id, metadata_by_id = audition.approved_identity_clips_by_id(manifest_path)

    expected_ids = {"identity-isabella", "identity-marco", "identity-giulia"}
    assert pack_digest == manifest["pack_digest"]
    assert set(paths_by_id) == expected_ids
    assert set(metadata_by_id) == expected_ids
    clips_by_id = {clip["id"]: clip for clip in manifest["clips"]}
    for clip_id in expected_ids:
        assert paths_by_id[clip_id] == run_dir / clips_by_id[clip_id]["path"]
        assert metadata_by_id[clip_id] == clips_by_id[clip_id]
    with pytest.raises(TypeError):
        metadata_by_id["identity-isabella"]["id"] = "identity-marco"  # type: ignore[index]


def test_approved_identity_clips_preserve_id_path_pairing_when_validated_order_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordered_ids = ["identity-giulia", "identity-isabella", "identity-marco"]
    clips = [{"id": clip_id, "audio_sha256": clip_id} for clip_id in ordered_ids]
    paths = tuple(tmp_path / f"{clip_id}.mp3" for clip_id in ordered_ids)
    manifest = {
        "pack_digest": "a" * 64,
        "listening_receipt": {"status": "approved"},
        "clips": clips,
    }
    monkeypatch.setattr(audition, "_validate_identity_board", lambda _path: (manifest, paths))

    digest, paths_by_id, metadata_by_id = audition.approved_identity_clips_by_id(tmp_path / "manifest.json")

    assert digest == "a" * 64
    assert paths_by_id == dict(zip(ordered_ids, paths, strict=True))
    assert {clip_id: metadata["audio_sha256"] for clip_id, metadata in metadata_by_id.items()} == {
        clip_id: clip_id for clip_id in ordered_ids
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pending", "rejected", "tampered"])
async def test_approved_identity_clips_fail_closed_for_unapproved_or_stale_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    run_dir, manifest = await _render_board(tmp_path, monkeypatch, stamp="20260815T180016Z")
    manifest_path = run_dir / "manifest.json"
    if failure == "rejected":
        audition.write_identity_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=_decision_path(
                tmp_path,
                manifest["pack_digest"],
                status="rejected",
                wrong="Marco",
                rationale="rejected_wrong_voice_identity",
            ),
            reviewed_at="2026-08-15T20:00:00+02:00",
        )
    elif failure == "tampered":
        audition.write_identity_listening_receipt_from_decision(
            manifest_path=manifest_path,
            decision_path=_decision_path(tmp_path, manifest["pack_digest"]),
            reviewed_at="2026-08-15T20:00:00+02:00",
        )
        first_clip = manifest["clips"][0]
        (run_dir / first_clip["path"]).write_bytes(b"tampered-after-approval")

    with pytest.raises(ValueError):
        audition.approved_identity_clips_by_id(manifest_path)

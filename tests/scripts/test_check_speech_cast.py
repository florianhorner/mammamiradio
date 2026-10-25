"""Contracts for scripts/check_speech_cast.py, the one-key speech cast gate."""

from __future__ import annotations

import datetime as dt
import json
from copy import deepcopy
from pathlib import Path

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.hosts.ad_creative import AdVoice
from scripts import audition_tts_voices as audition
from scripts import check_speech_cast as gate

ROOT = Path(__file__).resolve().parents[2]
RADIO_TOML = ROOT / "radio.toml"


def _row(cast_route: gate.CastRoute, *, status: str = "accepted") -> dict[str, object]:
    """A valid receipt row for ``cast_route``; only its candidate ID and status matter here."""
    return {
        "candidate_id": cast_route.candidate_id,
        "candidate_name": cast_route.label,
        "profile_fingerprint": "0" * 64,
        "text_sha256": "a" * 64,
        "provider_result": "generated",
        "audio_sha256": "b" * 64,
        "audio_duration_seconds": 30.0,
        "approval_status": status,
        "rationale": "accepted_distinct_character" if status == "accepted" else "rejected_unconvincing_character",
    }


def _receipt(routes: list[gate.CastRoute], *, rejected: frozenset[str] = frozenset()) -> dict[str, object]:
    rows: dict[str, dict[str, object]] = {}
    for cast_route in routes:
        status = "rejected" if cast_route.label in rejected else "accepted"
        rows.setdefault(cast_route.candidate_id, _row(cast_route, status=status))
    return audition.selection_receipt(list(rows.values()))


def _route(routes: list[gate.CastRoute], label: str) -> gate.CastRoute:
    return next(cast_route for cast_route in routes if cast_route.label == label)


# --- release window -----------------------------------------------------------


def test_review_date_keeps_the_interim_short():
    assert gate.DECISION_DATE < gate.REVIEW_BY
    assert (gate.REVIEW_BY - gate.DECISION_DATE).days <= gate.MAX_INTERIM_DAYS


@pytest.mark.parametrize(
    "today",
    [gate.DECISION_DATE, gate.REVIEW_BY - dt.timedelta(days=1), gate.REVIEW_BY],
    ids=["decision-day", "day-before-review", "review-day"],
)
def test_release_passes_inside_the_review_window(today, capsys):
    assert gate.main(["--release", "--today", today.isoformat()]) == gate.EXIT_OK
    assert "inside its review window" in capsys.readouterr().out


def test_release_fails_the_day_after_the_review_date(capsys):
    late = gate.REVIEW_BY + dt.timedelta(days=1)

    assert gate.main(["--release", "--today", late.isoformat()]) == gate.EXIT_FINDING
    out = capsys.readouterr().out
    assert f"passed its review date {gate.REVIEW_BY}" in out
    assert "Re-audition the cast" in out


def test_a_later_review_date_cannot_stretch_the_interim(monkeypatch):
    monkeypatch.setattr(gate, "REVIEW_BY", gate.DECISION_DATE + dt.timedelta(days=gate.MAX_INTERIM_DAYS + 1))

    findings = gate.release_findings(gate.DECISION_DATE)

    assert findings and "needs a new listening pass, not a later date" in findings[0]


def test_release_mode_reads_only_dates(tmp_path):
    """The release check runs on a bare Python, so it must not need the receipt or config."""
    missing = tmp_path / "missing.json"
    today = gate.DECISION_DATE.isoformat()
    assert gate.main(["--release", "--today", today, "--receipt", str(missing)]) == gate.EXIT_OK


@pytest.mark.parametrize(
    ("today", "expected"),
    [(gate.REVIEW_BY, gate.EXIT_OK), (gate.REVIEW_BY + dt.timedelta(days=1), gate.EXIT_FINDING)],
    ids=["review-day", "day-after"],
)
def test_release_mode_runs_as_a_bare_script_outside_the_repo(tmp_path, today, expected):
    """Section 12 calls the script directly, so it must run isolated, from any directory."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "scripts" / "check_speech_cast.py"),
            "--release",
            "--today",
            today.isoformat(),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected, result.stdout + result.stderr


# --- binding -----------------------------------------------------------------


def test_every_airtime_ad_voice_and_the_ident_are_gated_and_hosts_are_not():
    config = load_config(str(RADIO_TOML))

    labels = [cast_route.label for cast_route in gate.cast_routes(config)]

    expected = [f"ad voice {voice.name}" for voice in config.ads.voices if voice.airtime_approved]
    assert labels == [*expected, gate.IDENT_LABEL]
    assert not any(cast_route.label.startswith("host") for cast_route in gate.cast_routes(config))


def test_staged_candidates_need_no_listening_row():
    config = load_config(str(RADIO_TOML))
    staged = [voice.name for voice in config.ads.voices if not voice.airtime_approved]

    labels = {cast_route.label for cast_route in gate.cast_routes(config)}

    assert staged, "radio.toml should still stage candidates for this test to mean anything"
    assert not labels & {f"ad voice {name}" for name in staged}


@pytest.mark.parametrize("config_path", [RADIO_TOML, ROOT / "ha-addon" / "mammamiradio" / "radio.toml"])
def test_every_shipped_on_air_voice_has_an_accepted_row_in_the_committed_receipt(config_path):
    """The shipped cast airs only voices the listening pass accepted, in both copies."""
    receipt = audition.load_selection_receipt(gate.DEFAULT_RECEIPT)

    assert gate.binding_findings(load_config(str(config_path)), receipt) == []


def test_binding_passes_when_every_route_is_accepted():
    config = load_config(str(RADIO_TOML))
    receipt = _receipt(gate.cast_routes(config))

    assert gate.binding_findings(config, receipt) == []


@pytest.mark.parametrize("duplicate_first", [True, False])
@pytest.mark.parametrize("same_route", [True, False])
@pytest.mark.parametrize("duplicate_airs", [True, False])
def test_duplicate_ad_names_cannot_hide_an_unheard_route(duplicate_first, same_route, duplicate_airs):
    config = load_config(str(RADIO_TOML))
    receipt = audition.load_selection_receipt(gate.DEFAULT_RECEIPT)
    voice = next(voice for voice in config.ads.voices if voice.airtime_approved)
    duplicate = deepcopy(voice)
    duplicate.airtime_approved = duplicate_airs
    if not same_route:
        duplicate.voice = "unheard-voice-id"
    if duplicate_first:
        config.ads.voices.insert(0, duplicate)
    else:
        config.ads.voices.append(duplicate)

    findings = gate.binding_findings(config, receipt)

    assert len(findings) == 1
    assert voice.name in findings[0] and "duplicate" in findings[0]
    with pytest.raises(ValueError, match="duplicate"):
        gate.cast_routes(config)


def test_duplicate_staged_names_do_not_hold_the_airtime_cast():
    config = load_config(str(RADIO_TOML))
    staged = next(voice for voice in config.ads.voices if not voice.airtime_approved)
    config.ads.voices.append(deepcopy(staged))

    assert gate.binding_findings(config, audition.load_selection_receipt(gate.DEFAULT_RECEIPT)) == []


def test_cli_reports_duplicate_ad_names_as_a_finding(monkeypatch, capsys):
    config = load_config(str(RADIO_TOML))
    voice = next(voice for voice in config.ads.voices if voice.airtime_approved)
    config.ads.voices.append(deepcopy(voice))
    monkeypatch.setattr("mammamiradio.core.config.load_config", lambda _path: config)

    assert gate.main(["--binding"]) == gate.EXIT_FINDING
    assert "FAIL:" in capsys.readouterr().out


def test_a_route_without_a_row_is_named():
    config = load_config(str(RADIO_TOML))
    routes = gate.cast_routes(config)
    ident = _route(routes, gate.IDENT_LABEL)
    receipt = _receipt([cast_route for cast_route in routes if cast_route.candidate_id != ident.candidate_id])

    findings = gate.binding_findings(config, receipt)

    assert findings == [
        f"{gate.IDENT_LABEL} has no accepted listening row ({ident.provider} {ident.voice}); "
        "audition it and add it to the receipt"
    ]


def test_a_rejected_voice_cannot_air():
    config = load_config(str(RADIO_TOML))
    routes = gate.cast_routes(config)
    first_ad = routes[0]
    receipt = _receipt(routes, rejected=frozenset({first_ad.label}))

    findings = gate.binding_findings(config, receipt)

    assert f"{first_ad.label} airs on a voice the listening pass rejected" in "\n".join(findings)


def test_changing_a_voice_setting_needs_a_new_listening_row():
    config = load_config(str(RADIO_TOML))
    receipt = _receipt(gate.cast_routes(config))
    voice = next(voice for voice in config.ads.voices if voice.airtime_approved and voice.engine == "elevenlabs")
    voice.voice_settings = {"stability": 0.11}

    findings = gate.binding_findings(config, receipt)

    assert findings == [
        f"ad voice {voice.name} has no accepted listening row (elevenlabs {voice.voice}); "
        "audition it and add it to the receipt"
    ]


def test_an_ident_without_its_own_voice_is_a_finding():
    config = load_config(str(RADIO_TOML))
    receipt = _receipt(gate.cast_routes(config))
    config.sonic_brand.sweeper_voice = ""

    findings = gate.binding_findings(config, receipt)

    assert findings == [f"{gate.IDENT_LABEL} has no voice of its own; set [sonic_brand] sweeper_voice and audition it"]


@pytest.mark.parametrize(
    ("engine", "voice"),
    [("elevenlabs", "ident-candidate-voice"), ("azure", "it-IT-Isabella:DragonHDLatestNeural")],
)
def test_an_ident_candidate_staged_as_an_ad_voice_carries_the_ident_candidate_id(engine, voice):
    """The listening receipt only reads ad results, so ident candidates are staged as ad voices.

    With no settings, the staged ad voice and the ident route must render the same
    profile, or an accepted ident row could never bind.
    """
    config = load_config(str(RADIO_TOML))
    config.sonic_brand.sweeper_voice = voice
    config.sonic_brand.sweeper_engine = engine
    config.ads.voices.append(
        AdVoice(name="Ident Candidate", voice=voice, style="station announcer", engine=engine, role="hammer")
    )

    routes = gate.cast_routes(config)

    assert _route(routes, "ad voice Ident Candidate").candidate_id == _route(routes, gate.IDENT_LABEL).candidate_id


def test_unreadable_receipt_is_not_a_pass(tmp_path, capsys):
    broken = tmp_path / "receipt.json"
    broken.write_text("{not json", encoding="utf-8")

    assert gate.main(["--binding", "--receipt", str(broken)]) == gate.EXIT_UNREADABLE
    assert "could not be read" in capsys.readouterr().out


def test_a_receipt_that_fails_validation_is_not_a_pass(tmp_path, capsys):
    invalid = tmp_path / "receipt.json"
    invalid.write_text(json.dumps({"schema_version": 1, "candidates": []}), encoding="utf-8")

    assert gate.main(["--binding", "--receipt", str(invalid)]) == gate.EXIT_UNREADABLE
    assert "non-empty array" in capsys.readouterr().out


def test_cli_binding_reports_findings_and_passes(tmp_path, capsys):
    config = load_config(str(RADIO_TOML))
    routes = gate.cast_routes(config)
    complete = tmp_path / "complete.json"
    complete.write_text(json.dumps(_receipt(routes)), encoding="utf-8")
    partial = tmp_path / "partial.json"
    unheard = routes[0]
    heard = [cast_route for cast_route in routes if cast_route.candidate_id != unheard.candidate_id]
    partial.write_text(json.dumps(_receipt(heard)), encoding="utf-8")

    assert gate.main(["--binding", "--config", str(RADIO_TOML), "--receipt", str(complete)]) == gate.EXIT_OK
    assert "every on-air voice in radio.toml has an accepted listening row" in capsys.readouterr().out
    assert gate.main(["--binding", "--config", str(RADIO_TOML), "--receipt", str(partial)]) == gate.EXIT_FINDING
    assert f"FAIL: {unheard.label} has no accepted listening row" in capsys.readouterr().out

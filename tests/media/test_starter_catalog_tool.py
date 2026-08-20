from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "starter-catalog.py"
VALIDATOR = ROOT / "scripts" / "validate-starter-media.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("starter_catalog_tool", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_strict_check_stays_red_for_pending_human_auditions() -> None:
    result = subprocess.run(
        [sys.executable, os.fspath(SCRIPT), "check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "full-track human audition is pending" in result.stdout
    assert "approved derivatives 0/12" in result.stdout
    assert "source receipt is missing" not in result.stdout


def test_incomplete_check_is_green_only_as_explicit_scaffold_mode() -> None:
    result = subprocess.run(
        [sys.executable, os.fspath(SCRIPT), "check", "--allow-incomplete", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["ok"] is True
    assert report["allow_incomplete"] is True
    assert report["groups"]["MEDIA-EVIDENCE"]["status"] == "WARN"
    assert report["groups"]["MEDIA-PACKAGE"]["status"] == "PASS"


def test_validator_wrapper_forwards_scaffold_mode() -> None:
    result = subprocess.run(
        [sys.executable, os.fspath(VALIDATOR), "--allow-incomplete"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "MEDIA-EVIDENCE WARN" in result.stdout


@pytest.mark.parametrize(
    "url",
    [
        "http://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3",
        "https://evil.example/music/royalty-free/mp3-royaltyfree/Carefree.mp3",
        "https://user@incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3",
        "https://incompetech.com:444/music/royalty-free/mp3-royaltyfree/Carefree.mp3",
        "https://incompetech.com/other/Carefree.mp3",
    ],
)
def test_acquire_url_boundary_rejects_non_official_inputs(tool, url: str) -> None:
    with pytest.raises(tool.ToolError):
        tool._validate_official_url(url, audio=True)


def test_acquire_url_boundary_accepts_exact_official_directory(tool) -> None:
    tool._validate_official_url("https://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3", audio=True)
    tool._validate_piece_url(
        "https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1400037",
        isrc="USUAN1400037",
    )
    with pytest.raises(tool.ToolError, match="does not name exact ISRC"):
        tool._validate_piece_url(
            "https://incompetech.com/music/royalty-free/index.html?isrc=USUAN0000000",
            isrc="USUAN1400037",
        )


def test_acquire_rejects_isrc_not_predeclared_in_manifest(tool) -> None:
    with pytest.raises(tool.ToolError, match="not one exact canonical manifest row"):
        tool._catalog_row("USUAN0000000")


def test_duration_parser_rejects_malformed_official_value(tool) -> None:
    assert tool._duration_from_clock("00:03:25") == 205
    with pytest.raises(tool.ToolError, match="unexpected official duration"):
        tool._duration_from_clock("3:25")


def test_full_human_decision_is_required_and_redacted(tmp_path: Path, tool) -> None:
    decision = {
        "schema_version": "1",
        "isrc": "USUAN1400037",
        "reviewed_at": "2026-07-16T00:00:00Z",
        "reviewer_role": "project-maintainer",
        "listened_from_start_to_finish": True,
        "title_and_artist_match": True,
        "no_unexpected_speech_or_restricted_content": True,
        "editorially_approved": True,
        "license_evidence_reviewed": True,
    }
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(decision), encoding="utf-8")
    assert tool._validate_decisions(path, isrc="USUAN1400037") == decision

    decision["listened_from_start_to_finish"] = False
    path.write_text(json.dumps(decision), encoding="utf-8")
    with pytest.raises(tool.ToolError, match="human approval is incomplete"):
        tool._validate_decisions(path, isrc="USUAN1400037")

    decision["listened_from_start_to_finish"] = True
    decision["reviewer"] = "A Personal Name"
    path.write_text(json.dumps(decision), encoding="utf-8")
    with pytest.raises(tool.ToolError, match="reviewer_role"):
        tool._validate_decisions(path, isrc="USUAN1400037")

    decision.pop("reviewer")
    decision["reviewed_at"] = "not-a-timestampZ"
    path.write_text(json.dumps(decision), encoding="utf-8")
    with pytest.raises(tool.ToolError, match="valid ISO timestamp"):
        tool._validate_decisions(path, isrc="USUAN1400037")


def test_candidate_identifier_accepts_acquire_timestamp(tmp_path: Path, monkeypatch, tool) -> None:
    work = tmp_path / "starter-work"
    candidate = "usuan1400037-20260715T233405Z"
    (work / "candidates" / candidate).mkdir(parents=True)
    monkeypatch.setenv("MAMMAMIRADIO_STARTER_WORK_DIR", os.fspath(work))

    assert tool._candidate_dir(candidate).name == candidate
    with pytest.raises(tool.ToolError, match="identifier printed by acquire"):
        tool._candidate_dir("../escape")


def test_receipt_boundary_accepts_proof_and_rejects_escape(tool) -> None:
    assert tool._repo_receipt_path("proof/media/starter-source-evidence.json").is_file()
    with pytest.raises(tool.ToolError, match="unsafe media receipt path"):
        tool._repo_receipt_path("../outside.json")


def test_approved_audio_is_remeasured_for_loudness_and_duration(tmp_path: Path, monkeypatch, tool) -> None:
    entries = tuple(
        SimpleNamespace(
            isrc=f"USUAN990000{index}",
            path=tmp_path / f"track-{index}.mp3",
            duration_seconds=duration,
        )
        for index, duration in enumerate((1350.0, 1349.0))
    )
    probe_facts = {
        entry.path: {
            "codec": "mp3",
            "sample_rate_hz": 48000,
            "channels": 2,
            "bitrate_kbps": 192,
            "duration_seconds": entry.duration_seconds,
        }
        for entry in entries
    }
    loudness = iter((-16.0, -14.9))
    monkeypatch.setattr(tool, "_probe", lambda path: probe_facts[path])
    monkeypatch.setattr(tool, "_measure_lufs", lambda _path: next(loudness))

    measured_duration, failures = tool._measure_approved_entries(
        entries,
        expected_loudness_by_isrc={entries[0].isrc: -16.0, entries[1].isrc: -16.0},
    )

    assert measured_duration == 2699.0
    assert any("outside -17.0..-15.0 LUFS" in failure for failure in failures)
    assert any("loudness does not match the manifest" in failure for failure in failures)


def test_strict_check_uses_measured_duration_not_expected_duration(tmp_path: Path, monkeypatch, tool) -> None:
    isrc = "USUAN9900001"
    derivative = {
        "path": "tracks/fixture.mp3",
        "sha256": "b" * 64,
        "bytes": 100,
        "codec": "mp3",
        "sample_rate_hz": 48000,
        "channels": 2,
        "bitrate_kbps": 192,
        "duration_seconds": 3000.0,
        "integrated_loudness_lufs": -16.0,
    }
    catalog = {
        "schema_version": "1",
        "catalog_id": "starter-v1",
        "release_requirements": {
            "exact_track_count": 1,
            "minimum_duration_seconds": 2700,
            "maximum_payload_bytes": 1_000_000,
        },
        "tracks": [
            {
                "isrc": isrc,
                "expected_duration_seconds": 3000,
                "license": {"id": "CC-BY-4.0"},
                "source": {
                    "url": "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Fixture.mp3",
                    "evidence_url": f"https://incompetech.com/music/royalty-free/index.html?isrc={isrc}",
                    "sha256": "a" * 64,
                    "bytes": 200,
                },
                "evidence": {
                    "source_receipt": "proof/media/fixture-approval.json",
                    "audition_receipt": "proof/media/fixture-approval.json",
                },
                "approval": {"status": "approved"},
                "derivative": derivative,
            }
        ],
    }
    receipt = {
        "schema_version": "1",
        "receipt_type": "starter-track-approval",
        "isrc": isrc,
        "decisions": {field: True for field in tool.REQUIRED_DECISIONS},
        "source": {"sha256": "a" * 64, "bytes": 200},
        "derivative": {"sha256": "b" * 64},
    }
    catalog_path = tmp_path / "starter" / "catalog.json"
    catalog_path.parent.mkdir()
    catalog_path.write_text("{}", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('"assets/**/*"\n', encoding="utf-8")
    entry = SimpleNamespace(isrc=isrc)
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    monkeypatch.setattr(tool, "CATALOG_PATH", catalog_path)
    monkeypatch.setattr(tool, "_catalog_data", lambda: catalog)
    monkeypatch.setattr(tool, "_repo_receipt_path", lambda _path: tmp_path / "receipt.json")
    monkeypatch.setattr(tool, "_read_json", lambda _path: receipt)
    monkeypatch.setattr(
        tool,
        "load_starter_catalog",
        lambda **_kwargs: SimpleNamespace(entries=(entry,)),
    )
    monkeypatch.setattr(tool, "_measure_approved_entries", lambda *_args, **_kwargs: (2699.0, []))

    report = tool.check()

    assert report["groups"]["MEDIA-AUDIO"]["status"] == "FAIL"
    assert "measured approved duration 2699.000s is below 2700s" in report["groups"]["MEDIA-AUDIO"]["messages"]


@pytest.mark.requires_ffmpeg
def test_ebur128_measurement_accepts_a_real_in_tolerance_derivative(tmp_path: Path, tool) -> None:
    path = tmp_path / "in-tolerance.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-af",
            "volume=5dB",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            os.fspath(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    measured_lufs = tool._measure_lufs(path)

    assert measured_lufs >= tool.STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS
    assert measured_lufs <= tool.STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS


# --- Jamendo provider ---------------------------------------------------------
# Acquisition-path coverage. Deliberately independent of whichever crate ships:
# these drive the gates directly, so they keep working when the bundle changes.


@pytest.mark.parametrize(
    "license_url",
    [
        "http://creativecommons.org/licenses/by-nc-nd/3.0/",
        "http://creativecommons.org/licenses/by-nd/3.0/",
        "http://creativecommons.org/licenses/by-nc/3.0/",
        "http://creativecommons.org/licenses/by-nc-sa/3.0/",
        "http://creativecommons.org/licenses/by-sa/3.0/",
        "http://creativecommons.org/licenses/by/2.5/",
        "",
    ],
)
def test_jamendo_licence_gate_refuses_everything_but_attribution(tool, license_url: str) -> None:
    """ND is the one that matters: staging normalizes, which makes a derivative."""
    with pytest.raises(tool.ToolError, match="bundle cannot carry"):
        tool._require_jamendo_attribution_license(license_url, track_id="1")


@pytest.mark.parametrize(
    ("license_url", "expected"),
    [
        ("http://creativecommons.org/licenses/by/3.0/", "CC-BY-3.0"),
        ("https://creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
        # Jamendo reports http:// and sometimes omits the trailing slash.
        ("http://creativecommons.org/licenses/by/3.0", "CC-BY-3.0"),
    ],
)
def test_jamendo_licence_gate_accepts_attribution_only(tool, license_url: str, expected: str) -> None:
    assert tool._require_jamendo_attribution_license(license_url, track_id="1") == expected


@pytest.mark.parametrize(
    ("url", "audio"),
    [
        ("https://evil.example.com/track/1", False),
        ("http://www.jamendo.com/track/1", False),
        # A page host must never satisfy an audio fetch, and vice versa: this is
        # what stops a redirect walking a download onto an arbitrary endpoint.
        ("https://www.jamendo.com/track/1", True),
        ("https://prod-1.storage.jamendo.com/?trackid=1", False),
        ("https://user@www.jamendo.com/track/1", False),
        ("https://www.jamendo.com:444/track/1", False),
    ],
)
def test_jamendo_url_boundary_rejects_off_provider_inputs(tool, url: str, audio: bool) -> None:
    with pytest.raises(tool.ToolError):
        tool._validate_jamendo_url(url, audio=audio)


def test_jamendo_track_id_must_be_unambiguous(tool) -> None:
    assert tool._jamendo_track_id("https://www.jamendo.com/track/1093607/stitches", field="f") == "1093607"
    assert (
        tool._jamendo_track_id("https://prod-1.storage.jamendo.com/?trackid=1093607&format=mp31", field="f")
        == "1093607"
    )
    for bad in (
        "https://www.jamendo.com/artist/1093607/x",
        "https://prod-1.storage.jamendo.com/?trackid=abc",
        "https://prod-1.storage.jamendo.com/?trackid=1&trackid=2",
    ):
        with pytest.raises(tool.ToolError):
            tool._jamendo_track_id(bad, field="f")


def test_jamendo_empty_api_reply_is_retried_not_believed(tool, monkeypatch) -> None:
    """An empty result set is measured flakiness, never proof a track is absent.

    Measured 2026-08-20: the same id answered ``results_count=0`` on four of five
    consecutive calls. Treating that as authoritative would fail acquisition at
    random; treating it as "no restriction" would be far worse.
    """
    empty = json.dumps({"headers": {"status": "success"}, "results": []}).encode()
    good = json.dumps(
        {"headers": {"status": "success"}, "results": [{"id": "5", "name": "T", "artist_name": "A", "duration": 100}]}
    ).encode()
    replies = [empty, empty, good]
    monkeypatch.setattr(tool.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        tool, "_fetch", lambda *a, **k: (replies.pop(0), "https://api.jamendo.com/v3.0/tracks/", "json")
    )

    _raw, _url, official = tool._jamendo_track_facts("https://api.jamendo.com/v3.0/tracks/?id=5", track_id="5")

    assert official["id"] == "5"
    assert not replies, "should have consumed the empty replies before succeeding"


def test_jamendo_persistent_empty_reply_fails_closed(tool, monkeypatch) -> None:
    empty = json.dumps({"headers": {"status": "success"}, "results": []}).encode()
    monkeypatch.setattr(tool.time, "sleep", lambda _s: None)
    monkeypatch.setattr(tool, "_fetch", lambda *a, **k: (empty, "https://api.jamendo.com/v3.0/tracks/", "json"))

    with pytest.raises(tool.ToolError, match="never assumed permissive"):
        tool._jamendo_track_facts("https://api.jamendo.com/v3.0/tracks/?id=5", track_id="5")


def test_jamendo_api_error_is_not_retried(tool, monkeypatch) -> None:
    """A refusal is definite information; only emptiness is ambiguous."""
    calls = []

    def fake_fetch(*_a, **_k):
        calls.append(1)
        body = json.dumps({"headers": {"status": "failed", "error_message": "bad credential"}, "results": []})
        return body.encode(), "https://api.jamendo.com/v3.0/tracks/", "json"

    monkeypatch.setattr(tool, "_fetch", fake_fetch)
    with pytest.raises(tool.ToolError, match="bad credential"):
        tool._jamendo_track_facts("https://api.jamendo.com/v3.0/tracks/?id=5", track_id="5")
    assert len(calls) == 1


def test_credential_never_appears_in_an_error_message(tool) -> None:
    """The likeliest Jamendo failure is a 401 from a stale key.

    That is exactly the message a maintainer pastes into an issue or a chat, and
    the client id travels in the query string, so every `_fetch` error path would
    have carried it verbatim.
    """
    leaky = "https://api.jamendo.com/v3.0/tracks/?client_id=a1b2c3d4&format=json&id=1215805"
    redacted = tool._redact(leaky)

    assert "a1b2c3d4" not in redacted
    assert "<redacted>" in redacted
    # The diagnostic must survive the scrub or the message stops being useful.
    assert "id=1215805" in redacted
    assert tool._redact("https://x/?format=json&client_id=deadbeef").endswith("<redacted>")


@pytest.mark.parametrize(
    "url",
    [
        # Reads as track 1215805, opens track 999 in a browser. The human ticking
        # "licence evidence reviewed" would audit a different track than the one
        # whose licence was proven.
        "https://www.jamendo.com/track/1215805/../../track/999",
        "https://www.jamendo.com/track/999/slug?trackid=1215805",
        "https://www.jamendo.com/track//1215805",
    ],
)
def test_jamendo_page_url_cannot_split_the_audit_trail(tool, url: str) -> None:
    with pytest.raises(tool.ToolError):
        tool._jamendo_track_id(url, field="source.evidence_url")


def test_jamendo_audio_url_is_path_locked_like_incompetech(tool) -> None:
    """Incompetech audio URLs are locked to a download directory; so are these."""
    tool._validate_jamendo_url("https://prod-1.storage.jamendo.com/?trackid=1215805&format=mp31", audio=True)
    with pytest.raises(tool.ToolError, match="storage root"):
        tool._validate_jamendo_url("https://prod-1.storage.jamendo.com/track/999/audio.mp3?trackid=1215805", audio=True)


def test_licence_allowlist_is_scoped_per_provider(tool) -> None:
    """Incompetech publishes 4.0 only — a 3.0 claim there would be a false notice."""
    assert tool.PROVIDER_ALLOWED_LICENSE_IDS[tool.PROVIDER_INCOMPETECH] == {"CC-BY-4.0"}
    assert "CC-BY-3.0" in tool.PROVIDER_ALLOWED_LICENSE_IDS[tool.PROVIDER_JAMENDO]


@pytest.mark.parametrize(
    "envelope",
    ['{"headers":[{"status":"success"}],"results":[]}', '{"headers":"success","results":[]}', "[1,2,3]"],
)
def test_malformed_api_envelope_is_an_actionable_error_not_a_traceback(tool, monkeypatch, envelope: str) -> None:
    monkeypatch.setattr(tool.time, "sleep", lambda _s: None)
    monkeypatch.setattr(tool, "_fetch", lambda *a, **k: (envelope.encode(), tool.JAMENDO_API_URL, "json"))
    with pytest.raises(tool.ToolError):
        tool._jamendo_track_facts(f"{tool.JAMENDO_API_URL}?id=5", track_id="5")


def _jamendo_row(**overrides):
    row = {
        "isrc": "JAMENDO-1093607",
        "provider": "jamendo",
        "title": "Stitches ft. Shane MauX",
        "artist": "Lilly Wolf",
        "expected_duration_seconds": 270,
        "official_piece_url": "https://www.jamendo.com/track/1093607",
        "license": {"id": "CC-BY-3.0", "url": "https://creativecommons.org/licenses/by/3.0/"},
        "source": {
            "url": "https://prod-1.storage.jamendo.com/?trackid=1093607&format=mp31",
            "filename": "1093607.mp3",
        },
    }
    row.update(overrides)
    return row


def _api_facts(**overrides):
    facts = {
        "id": "1093607",
        "name": "Stitches ft. Shane MauX",
        "artist_name": "Lilly Wolf",
        "duration": 270,
        "license_ccurl": "http://creativecommons.org/licenses/by/3.0/",
    }
    facts.update(overrides)
    return facts


@pytest.mark.parametrize(
    ("api_override", "expected"),
    [
        # The manifest says CC BY 3.0; Jamendo says NonCommercial. The manifest is
        # what generates the shipped attribution notice, so accepting this would
        # print a licence the track does not carry.
        ({"license_ccurl": "http://creativecommons.org/licenses/by-nc/3.0/"}, "bundle cannot carry"),
        ({"license_ccurl": "http://creativecommons.org/licenses/by-nd/3.0/"}, "bundle cannot carry"),
        # Attribution-only, but not the version the row declares.
        ({"license_ccurl": "https://creativecommons.org/licenses/by/4.0/"}, "but Jamendo reports"),
        # Identity drift: the row and the provider disagree about what this is.
        ({"name": "Some Other Track"}, "official title changed"),
        ({"artist_name": "Someone Else"}, "official artist changed"),
        ({"duration": 999}, "official duration changed"),
    ],
)
def test_acquire_refuses_when_the_manifest_and_jamendo_disagree(
    tool, monkeypatch, tmp_path: Path, api_override, expected
) -> None:
    """The manifest may not assert a licence the provider does not confirm.

    This is the only check binding the row's licence claim to what Jamendo
    actually reports, and it had no test — deleting it passed the whole suite.
    A hand-edited row could otherwise ship a false licence notice on real audio.
    """
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "testkey1")
    monkeypatch.setattr(tool, "_work_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "_jamendo_track_facts",
        lambda _url, *, track_id: (b"{}", tool.JAMENDO_API_URL, _api_facts(**api_override)),
    )
    # Audio must never be fetched: the refusal has to happen before download.
    monkeypatch.setattr(
        tool, "_fetch", lambda *a, **k: pytest.fail("audio was fetched despite a licence/identity mismatch")
    )

    with pytest.raises(tool.ToolError, match=expected):
        tool._acquire_jamendo(_jamendo_row(), "JAMENDO-1093607")


def test_acquire_requires_a_credential_and_says_where_to_get_one(tool, monkeypatch) -> None:
    monkeypatch.delenv("JAMENDO_CLIENT_ID", raising=False)
    with pytest.raises(tool.ToolError, match=r"devportal\.jamendo\.com"):
        tool._acquire_jamendo(_jamendo_row(), "JAMENDO-1093607")


def test_acquire_refuses_a_row_whose_page_and_audio_name_different_tracks(tool, monkeypatch) -> None:
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "testkey1")
    row = _jamendo_row(
        source={
            "url": "https://prod-1.storage.jamendo.com/?trackid=999&format=mp31",
            "filename": "999.mp3",
        }
    )
    with pytest.raises(tool.ToolError, match="different one for audio"):
        tool._acquire_jamendo(row, "JAMENDO-1093607")


def test_provider_defaults_to_incompetech_and_rejects_unknown(tool) -> None:
    assert tool._provider_of({}) == "incompetech"
    assert tool._provider_of({"provider": "jamendo"}) == "jamendo"
    with pytest.raises(tool.ToolError, match="unknown provider"):
        tool._provider_of({"provider": "soundcloud"})

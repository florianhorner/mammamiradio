"""Packaged speech must be reviewed, content-addressed, and truth-safe."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mammamiradio.core import spoken_assets
from mammamiradio.core.spoken_assets import (
    approved_spoken_asset_entries,
    approved_spoken_assets,
    is_approved_packaged_audio_asset,
    is_approved_spoken_asset,
    validate_spoken_asset_manifest,
)


def _write_manifest(root, entries):
    (root / "spoken_assets.json").write_text(
        json.dumps({"schema_version": 1, "assets": entries}),
        encoding="utf-8",
    )


def _entry(path, payload, *, transcript="The station stays on air.", kind="speech", language="en"):
    return {
        "path": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "kind": kind,
        "language": language,
        "transcript": transcript,
    }


def test_shipped_manifest_is_valid_and_declares_reviewed_spoken_assets():
    assert validate_spoken_asset_manifest() == []
    recovery = approved_spoken_assets("recovery")
    first_listen = approved_spoken_assets("first_listen")
    banter = approved_spoken_asset_entries("banter")
    assert [path.name for path in recovery] == ["continuity_1.mp3"]
    assert sorted(path.name for path in first_listen) == ["first_listen_admin_show.mp3", "first_listen_show.mp3"]
    openings = {Path(entry.relative_path).name: entry for entry in approved_spoken_asset_entries("first_listen")}
    assert openings["first_listen_admin_show.mp3"].language == "en"
    assert openings["first_listen_show.mp3"].language == "it"
    assert (
        hashlib.sha256(
            next(path for path in first_listen if path.name == "first_listen_show.mp3").read_bytes()
        ).hexdigest()
        == "f03a1dc3184f9f108ae502a27e89ca7eebf626ee54cc28e555a63a5b08ab7af8"
    )
    assert len(banter) == 21
    assert sum(entry.mode == "normal" for entry in banter) == 15
    assert sum(entry.mode == "super_italian" for entry in banter) == 6
    assert sum(bool(entry.required_previous_starter_id) for entry in banter) == 3
    assert {entry.required_previous_starter_id for entry in banter if entry.required_previous_starter_id} == {
        "JAMENDO-1215805",
        "USUAN1100173",
    }
    starter_catalog = json.loads(
        (Path(__file__).resolve().parents[2] / "mammamiradio" / "assets" / "starter" / "catalog.json").read_text(
            encoding="utf-8"
        )
    )
    starter_ids = {str(entry["isrc"]) for entry in starter_catalog["tracks"]}
    assert {entry.required_previous_starter_id for entry in banter if entry.required_previous_starter_id} <= starter_ids
    assert sum(entry.special for entry in banter) == 3
    assert is_approved_spoken_asset(recovery[0]) is True
    assert is_approved_spoken_asset(first_listen[0]) is True


def test_missing_manifest_and_unlisted_audio_fail_closed(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    clip = recovery / "mystery.mp3"
    clip.write_bytes(b"x" * 2048)

    assert approved_spoken_assets("recovery", assets_root=tmp_path) == []
    _write_manifest(tmp_path, [])
    errors = validate_spoken_asset_manifest(assets_root=tmp_path)
    assert any("unlisted packaged audio" in error for error in errors)


def test_changed_hash_fails_closed_even_after_path_was_approved(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    clip = recovery / "continuity.mp3"
    original = b"reviewed" * 300
    clip.write_bytes(original)
    _write_manifest(tmp_path, [_entry("recovery/continuity.mp3", original)])
    assert is_approved_spoken_asset(clip, assets_root=tmp_path) is True

    clip.write_bytes(b"changed" * 300)
    assert is_approved_spoken_asset(clip, assets_root=tmp_path) is False
    assert any("sha256 does not match" in error for error in validate_spoken_asset_manifest(assets_root=tmp_path))


def test_listener_arrival_transcript_is_rejected(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    clip = recovery / "unsafe.mp3"
    payload = b"unsafe" * 400
    clip.write_bytes(payload)
    _write_manifest(
        tmp_path,
        [_entry("recovery/unsafe.mp3", payload, transcript="Someone just tuned in.")],
    )

    assert approved_spoken_assets("recovery", assets_root=tmp_path) == []
    assert is_approved_spoken_asset(clip, assets_root=tmp_path) is False
    assert any("listener arrival/return" in error for error in validate_spoken_asset_manifest(assets_root=tmp_path))


def test_runtime_admission_hashes_only_the_selected_asset(tmp_path, monkeypatch):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    selected = recovery / "selected.mp3"
    unrelated = recovery / "unrelated.mp3"
    selected_payload = b"selected" * 400
    unrelated_payload = b"unrelated" * 400
    selected.write_bytes(selected_payload)
    unrelated.write_bytes(unrelated_payload)
    _write_manifest(
        tmp_path,
        [
            _entry("recovery/selected.mp3", selected_payload),
            _entry("recovery/unrelated.mp3", unrelated_payload),
        ],
    )
    hashed_paths: list[Path] = []

    def _record_hash(path: Path) -> str:
        hashed_paths.append(Path(path))
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    monkeypatch.setattr(spoken_assets, "_sha256", _record_hash)

    assert is_approved_spoken_asset(selected, assets_root=tmp_path) is True
    assert hashed_paths == [selected]


def test_banter_metadata_is_mode_safe_and_specials_are_evergreen(tmp_path):
    banter = tmp_path / "banter"
    banter.mkdir()
    clip = banter / "unsafe-mode.mp3"
    payload = b"reviewed" * 400
    clip.write_bytes(payload)
    entry = _entry("banter/unsafe-mode.mp3", payload, language="it")
    entry.update(
        {
            "mode": "normal",
            "required_previous_starter_id": "TRACK-ID",
            "special": True,
        }
    )
    _write_manifest(tmp_path, [entry])

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)

    assert any("language does not match" in error for error in errors)
    assert any("special banter must be evergreen" in error for error in errors)
    assert approved_spoken_asset_entries("banter", assets_root=tmp_path) == []


@pytest.mark.parametrize(
    ("relative_path", "metadata", "expected_error"),
    [
        (
            "banter/clip.mp3",
            {"mode": "festival", "required_previous_starter_id": "", "special": False},
            "banter mode must be",
        ),
        (
            "banter/clip.mp3",
            {"mode": "normal", "required_previous_starter_id": "bad id", "special": False},
            "required starter id is invalid",
        ),
        (
            "recovery/clip.mp3",
            {"mode": "normal", "required_previous_starter_id": "", "special": False},
            "non-banter asset has banter metadata",
        ),
        (
            "banter/clip.mp3",
            {"mode": "normal", "required_previous_starter_id": "", "special": "yes"},
            "banter metadata has invalid types",
        ),
    ],
)
def test_invalid_banter_metadata_fails_closed(tmp_path, relative_path, metadata, expected_error):
    asset_dir = tmp_path / Path(relative_path).parent
    asset_dir.mkdir(parents=True)
    payload = b"reviewed" * 400
    (tmp_path / relative_path).write_bytes(payload)
    entry = _entry(relative_path, payload)
    entry.update(metadata)
    _write_manifest(tmp_path, [entry])

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)

    assert any(expected_error in error for error in errors)


def test_manifested_tone_is_inventory_valid_but_not_spoken(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    tone = recovery / "tone.mp3"
    payload = b"tone" * 600
    tone.write_bytes(payload)
    _write_manifest(
        tmp_path,
        [_entry("recovery/tone.mp3", payload, transcript="", kind="tone", language="none")],
    )

    assert validate_spoken_asset_manifest(assets_root=tmp_path) == []
    assert approved_spoken_assets("recovery", assets_root=tmp_path) == []
    assert is_approved_packaged_audio_asset(tone, assets_root=tmp_path) is True
    assert is_approved_spoken_asset(tone, assets_root=tmp_path) is False

    tone.write_bytes(b"tampered" * 600)
    assert is_approved_packaged_audio_asset(tone, assets_root=tmp_path) is False


def test_local_review_welcome_clip_does_not_invalidate_recovery_manifest(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    clip = recovery / "continuity.mp3"
    payload = b"reviewed" * 300
    clip.write_bytes(payload)
    welcome = tmp_path / "welcome"
    welcome.mkdir()
    (welcome / "local-review.mp3").write_bytes(b"local review only")
    _write_manifest(tmp_path, [_entry("recovery/continuity.mp3", payload)])

    assert validate_spoken_asset_manifest(assets_root=tmp_path) == []
    assert is_approved_spoken_asset(clip, assets_root=tmp_path) is True
    assert approved_spoken_assets("welcome", assets_root=tmp_path) == []


def test_symlink_loop_fails_closed_instead_of_raising(tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    loop = recovery / "loop.mp3"
    loop.symlink_to(loop.name)
    _write_manifest(
        tmp_path,
        [
            {
                "path": "recovery/loop.mp3",
                "sha256": "0" * 64,
                "kind": "speech",
                "language": "en",
                "transcript": "The station stays on air.",
            }
        ],
    )

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)

    assert any("escapes the asset root" in error for error in errors)
    assert is_approved_spoken_asset(loop, assets_root=tmp_path) is False
    # Second containment site: the per-entry lookup resolved the candidate
    # independently and had the same raise-dependent hole. Manifest validation
    # passing is not proof this path is guarded.
    assert is_approved_packaged_audio_asset(loop, assets_root=tmp_path) is False


def test_symlink_loop_is_rejected_without_relying_on_resolve_raising(tmp_path):
    """Containment must not depend on Path.resolve() raising on a cycle.

    On 3.12 and earlier a loop raised RuntimeError from resolve() and the
    containment check caught it incidentally. On 3.13 and later resolve()
    returns the unresolved path
    and raises nothing. Pin the behaviour rather than the mechanism, so the
    guard cannot silently stop firing on a future interpreter.
    """

    recovery = tmp_path / "recovery"
    recovery.mkdir()
    loop = recovery / "loop.mp3"
    loop.symlink_to(loop.name)

    # The precondition the old guard leaned on may or may not hold.
    try:
        loop.resolve()
        resolve_raised = False
    except (OSError, RuntimeError, ValueError):
        resolve_raised = True

    assert spoken_assets._stays_inside_root(loop, tmp_path) is False, (
        f"symlink cycle must be refused whether or not resolve() raised (resolve raised: {resolve_raised})"
    )


def test_dangling_symlink_is_not_reported_as_escaping_the_root(tmp_path):
    """Only a cycle is a containment failure; a missing target is not.

    Reporting a dangling link as an escape would replace the accurate
    downstream digest error with a misleading one.
    """

    recovery = tmp_path / "recovery"
    recovery.mkdir()
    dangling = recovery / "gone.mp3"
    dangling.symlink_to("also-gone.mp3")

    assert spoken_assets._stays_inside_root(dangling, tmp_path) is True


def test_unreadable_manifested_asset_fails_closed_instead_of_raising(tmp_path, monkeypatch):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    clip = recovery / "continuity.mp3"
    payload = b"reviewed" * 300
    clip.write_bytes(payload)
    _write_manifest(tmp_path, [_entry("recovery/continuity.mp3", payload)])
    monkeypatch.setattr(spoken_assets, "_sha256", lambda _path: (_ for _ in ()).throw(OSError("denied")))

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)

    assert any("is unreadable: denied" in error for error in errors)
    assert approved_spoken_assets("recovery", assets_root=tmp_path) == []


_MP3_FRAME_SECONDS = 1152 / 44100


def _mp3_frame(payload=b""):
    """One MPEG-1 Layer III frame: 128 kbps, 44.1 kHz, stereo, 417 bytes."""
    body = payload.ljust(413, b"\x00")
    return b"\xff\xfb\x90\x00" + body


def _mp3_bytes(seconds, *, id3=False, xing=False, id3v1=False, gapless=None):
    frames = b"".join(_mp3_frame() for _ in range(round(seconds / _MP3_FRAME_SECONDS)))
    if xing or gapless:
        # Stereo MPEG-1 side info is 32 bytes; the tag sits right after it.
        info = b"\x00" * 32 + b"Info" + b"\x00\x00\x00\x01" + b"\x00" * 4
        if gapless:
            delay, padding = gapless
            info += b"LAME3.100" + b"\x00" * 12 + ((delay << 12) | padding).to_bytes(3, "big")
        frames = _mp3_frame(info) + frames
    if id3:
        frames = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10 + frames
    if id3v1:
        frames += b"TAG" + b"\x00" * 125
    return frames


def _ad_entry(path, payload, *, mode="normal", language="en", duration=30.0, **overrides):
    entry = _entry(path, payload, language=language, transcript="A finished thirty-second spot for a fictional brand.")
    entry["mode"] = mode
    entry["duration_seconds"] = duration
    entry["title"] = "The Studio B Parcel Desk"
    entry["cast"] = ["Announcer", "Parcel clerk"]
    entry.update(overrides)
    return entry


def _ad_root(tmp_path, *, duration=30.0, audio_seconds=None, payload=None, **overrides):
    (tmp_path / "ads").mkdir()
    if payload is None:
        audio = duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) else 30.0
        if audio_seconds is not None:
            audio = audio_seconds
        payload = _mp3_bytes(audio if 0 < audio < 1000 else 30.0)
    (tmp_path / "ads" / "spot.mp3").write_bytes(payload)
    _write_manifest(tmp_path, [_ad_entry("ads/spot.mp3", payload, duration=duration, **overrides)])
    return tmp_path


def test_a_packaged_ad_of_ordinary_length_is_admitted(tmp_path):
    root = _ad_root(tmp_path)
    assert validate_spoken_asset_manifest(assets_root=root) == []
    entries = approved_spoken_asset_entries("ads", assets_root=root)
    assert [entry.relative_path for entry in entries] == ["ads/spot.mp3"]
    assert entries[0].duration_seconds == 30.0
    assert entries[0].title == "The Studio B Parcel Desk"
    assert entries[0].cast == ("Announcer", "Parcel clerk")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"title": ""}, "must declare a title"),
        ({"title": "x" * 81}, "title exceeds 80 characters"),
        ({"cast": []}, "must declare a cast"),
        ({"cast": ["x" * 41]}, "cast members must be 1-40 characters"),
        ({"cast": "Announcer"}, "cast must be a list of strings"),
    ],
)
def test_packaged_ad_title_and_cast_are_bounded(tmp_path, overrides, expected):
    root = _ad_root(tmp_path, **overrides)

    errors = validate_spoken_asset_manifest(assets_root=root)

    assert any(expected in error for error in errors), errors
    assert approved_spoken_asset_entries("ads", assets_root=root) == []


@pytest.mark.parametrize("duration", [2.0, 24.9, 40.1, 90.0])
def test_an_ad_outside_the_spot_length_is_refused(tmp_path, duration):
    """The shipped no-key fallback spoke a brand name and a tagline and aired.

    A two-second render is a successful render of copy that was never an
    advertisement. The packaging contract is where that gets caught.
    """
    root = _ad_root(tmp_path, duration=duration)
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert any("a packaged ad runs 25-40s" in error for error in errors), errors
    assert approved_spoken_asset_entries("ads", assets_root=root) == []


def test_an_ad_without_a_declared_duration_is_refused(tmp_path):
    (tmp_path / "ads").mkdir()
    payload = _mp3_bytes(30.0)
    (tmp_path / "ads" / "spot.mp3").write_bytes(payload)
    entry = _ad_entry("ads/spot.mp3", payload)
    del entry["duration_seconds"]
    _write_manifest(tmp_path, [entry])
    errors = validate_spoken_asset_manifest(assets_root=tmp_path)
    assert any("must declare duration_seconds" in error for error in errors), errors


def test_a_boolean_duration_cannot_pass_as_one_second(tmp_path):
    """``bool`` is an ``int`` subclass; ``True`` must not read as 1.0."""
    root = _ad_root(tmp_path, duration=True)
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert any("duration_seconds must be a number" in error for error in errors), errors


def test_an_ad_recorded_in_the_wrong_language_for_its_mode_is_refused(tmp_path):
    root = _ad_root(tmp_path, mode="super_italian", language="en")
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert any("ad language does not match its mode" in error for error in errors), errors


def test_an_ad_without_a_mode_is_refused(tmp_path):
    root = _ad_root(tmp_path, mode="")
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert any("ad mode must be normal or super_italian" in error for error in errors), errors


def test_an_ad_may_not_borrow_banter_adjacency(tmp_path):
    """An ad never depends on the song before it, and has no rare-special tier."""
    root = _ad_root(tmp_path, required_previous_starter_id="USUAN1100173", special=True)
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert any("must not carry banter adjacency metadata" in error for error in errors), errors


def test_undeclared_audio_in_the_ads_directory_fails_closed(tmp_path):
    root = _ad_root(tmp_path)
    (root / "ads" / "smuggled.mp3").write_bytes(b"not-declared")
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert any("ads/smuggled.mp3 is unlisted packaged audio" in error for error in errors), errors


def test_declaring_a_duration_leaves_other_categories_unchanged(tmp_path):
    """Banter and recovery never declared a length; adding the field is additive."""
    (tmp_path / "recovery").mkdir()
    payload = b"continuity"
    (tmp_path / "recovery" / "continuity_1.mp3").write_bytes(payload)
    _write_manifest(tmp_path, [_entry("recovery/continuity_1.mp3", payload, language="it")])
    assert validate_spoken_asset_manifest(assets_root=tmp_path) == []
    entries = approved_spoken_asset_entries("recovery", assets_root=tmp_path)
    assert entries[0].duration_seconds == 0.0


@pytest.mark.parametrize("duration", [25.0, 40.0, 25, 40])
def test_an_ad_exactly_at_either_limit_is_admitted(tmp_path, duration):
    """Both bounds are inclusive; integers are as valid as floats."""
    root = _ad_root(tmp_path, duration=duration)
    assert validate_spoken_asset_manifest(assets_root=root) == []
    assert len(approved_spoken_asset_entries("ads", assets_root=root)) == 1


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_duration_is_refused(tmp_path, duration):
    root = _ad_root(tmp_path, duration=duration)
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert any("non-negative finite number" in error for error in errors), errors


def _huge_duration_manifest(root, entry):
    """Write a manifest whose entry declares an integer too large for a float."""
    text = json.dumps(entry)
    huge = text[:-1] + ', "duration_seconds": 1' + "0" * 400 + "}"
    (root / "spoken_assets.json").write_text('{"schema_version": 1, "assets": [' + huge + "]}", encoding="utf-8")


def test_an_unrepresentable_ad_duration_is_refused_without_raising(tmp_path):
    """JSON integers are unbounded and ``float()`` raises on one too large."""
    (tmp_path / "ads").mkdir()
    payload = _mp3_bytes(30.0)
    (tmp_path / "ads" / "spot.mp3").write_bytes(payload)
    entry = _ad_entry("ads/spot.mp3", payload)
    del entry["duration_seconds"]
    _huge_duration_manifest(tmp_path, entry)

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)
    assert any("non-negative finite number" in error for error in errors), errors
    assert approved_spoken_asset_entries("ads", assets_root=tmp_path) == []


def test_a_bad_duration_cannot_drop_a_recovery_clip_from_the_rescue_ladder(tmp_path):
    """Only ads read a duration. Elsewhere the field stays inert, as it was.

    The rescue ladder asks this parser which packaged audio it may use, and a
    typo in a field recovery never reads must not remove a clip from it.
    """
    (tmp_path / "recovery").mkdir()
    payload = b"continuity"
    clip = tmp_path / "recovery" / "continuity_1.mp3"
    clip.write_bytes(payload)
    _huge_duration_manifest(tmp_path, _entry("recovery/continuity_1.mp3", payload, language="it"))

    assert validate_spoken_asset_manifest(assets_root=tmp_path) == []
    assert is_approved_packaged_audio_asset(clip, assets_root=tmp_path) is True

    _write_manifest(
        tmp_path, [{**_entry("recovery/continuity_1.mp3", payload, language="it"), "duration_seconds": "15s"}]
    )
    assert is_approved_packaged_audio_asset(clip, assets_root=tmp_path) is True


def test_a_manifest_integer_too_long_to_parse_fails_closed_instead_of_raising(tmp_path):
    """``json.loads`` raises a plain ValueError past 4300 digits, not a decode error."""
    (tmp_path / "recovery").mkdir()
    clip = tmp_path / "recovery" / "continuity_1.mp3"
    clip.write_bytes(b"continuity")
    (tmp_path / "spoken_assets.json").write_text(
        '{"schema_version": 1' + "0" * 5000 + ', "assets": []}', encoding="utf-8"
    )

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)
    assert any("unreadable" in error for error in errors), errors
    assert is_approved_packaged_audio_asset(clip, assets_root=tmp_path) is False


def test_a_packaged_ad_in_a_subfolder_is_refused(tmp_path):
    """Discovery and selection read ads/ flat; a nested spot would escape both."""
    (tmp_path / "ads" / "normal").mkdir(parents=True)
    payload = _mp3_bytes(2.0)
    (tmp_path / "ads" / "normal" / "spot.mp3").write_bytes(payload)
    _write_manifest(tmp_path, [_ad_entry("ads/normal/spot.mp3", payload, duration=2.0)])

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)
    assert any("must sit directly in ads/" in error for error in errors), errors
    assert any("a packaged ad runs 25-40s" in error for error in errors), errors


def test_an_ad_whose_audio_is_shorter_than_declared_is_refused(tmp_path):
    """The declared length is a claim; a two-second file cannot wear a 30s label."""
    root = _ad_root(tmp_path, duration=30.0, audio_seconds=2.0)
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert any("declares 30s but the audio runs 2." in error for error in errors), errors
    assert any("audio runs 2.011s; a packaged ad runs 25-40s" in error for error in errors), errors
    # The release boundary is the proof; runtime admission stays hash-only.
    assert len(approved_spoken_asset_entries("ads", assets_root=root)) == 1


def test_an_ad_within_range_but_off_its_declaration_is_refused(tmp_path):
    root = _ad_root(tmp_path, duration=30.0, audio_seconds=35.0)
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert errors == [next(error for error in errors if "declares 30s but the audio runs 35." in error)], errors


def test_an_ad_within_one_frame_of_its_declaration_is_admitted(tmp_path):
    root = _ad_root(tmp_path, duration=30.05, audio_seconds=30.0)
    assert validate_spoken_asset_manifest(assets_root=root) == []


@pytest.mark.parametrize(
    "payload",
    [
        b"packaged-ad",
        b"",
        b"\xff\xfb\x90\x00" + b"\x00" * 100,  # truncated frame
        _mp3_bytes(30.0) + b"junk",  # trailing bytes after the last frame
        b"\xff\xfb\xf0\x00" + b"\x00" * 413,  # bitrate index 15 is invalid
        b"\xff\xfd\x90\x00" + b"\x00" * 413,  # Layer II, not Layer III
        b"ID3\x04\x00\x00\x00\x00\x00\x80" + _mp3_bytes(30.0),  # size is not syncsafe
    ],
)
def test_ad_audio_that_is_not_decodable_mp3_fails_closed(tmp_path, payload):
    root = _ad_root(tmp_path, payload=payload)
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert any("ads/spot.mp3 is not decodable MP3 audio" in error for error in errors), errors


def test_an_oversized_ad_file_is_refused_before_it_is_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(spoken_assets, "PACKAGED_AD_MAX_BYTES", 1024)
    root = _ad_root(tmp_path)
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert any("file exceeds 1024 bytes" in error for error in errors), errors


def test_a_missing_ad_file_fails_closed(tmp_path):
    (tmp_path / "ads").mkdir()
    _write_manifest(tmp_path, [_ad_entry("ads/spot.mp3", _mp3_bytes(30.0))])
    errors = validate_spoken_asset_manifest(assets_root=tmp_path)
    assert errors == ["ads/spot.mp3 is missing"]
    assert approved_spoken_asset_entries("ads", assets_root=tmp_path) == []


def test_an_unreadable_ad_file_fails_closed_instead_of_raising(tmp_path, monkeypatch):
    root = _ad_root(tmp_path)
    real_open = Path.open

    def guarded_open(self, *args, **kwargs):
        if self.name == "spot.mp3":
            raise PermissionError("denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    errors = validate_spoken_asset_manifest(assets_root=root)
    assert errors.count("ads/spot.mp3 is unreadable: denied") == 2, errors
    assert approved_spoken_asset_entries("ads", assets_root=root) == []


@pytest.mark.parametrize(
    "options",
    [{"id3": True}, {"xing": True}, {"id3v1": True}, {"id3": True, "xing": True, "id3v1": True}],
)
def test_tags_and_the_info_header_are_not_counted_as_audio(tmp_path, options):
    path = tmp_path / "spot.mp3"
    path.write_bytes(_mp3_bytes(30.0, **options))
    expected = round(30.0 / _MP3_FRAME_SECONDS) * _MP3_FRAME_SECONDS
    assert spoken_assets._mp3_duration_seconds(path) == pytest.approx(expected)


def test_encoder_delay_and_padding_are_trimmed_like_ffprobe_does(tmp_path):
    path = tmp_path / "spot.mp3"
    path.write_bytes(_mp3_bytes(30.0, gapless=(576, 1000)))
    expected = (round(30.0 / _MP3_FRAME_SECONDS) * 1152 - 1576) / 44100
    assert spoken_assets._mp3_duration_seconds(path) == pytest.approx(expected)


def _mpeg2_mono_frame(payload=b""):
    """One MPEG-2 Layer III frame: 64 kbps, 22.05 kHz, mono, 208 bytes."""
    return b"\xff\xf3\x80\xc0" + payload.ljust(204, b"\x00")


def _mpeg25_mono_frame():
    """One MPEG-2.5 Layer III frame: 8 kbps, 8 kHz, mono, 72 bytes."""
    return b"\xff\xe3\x18\xc0" + b"\x00" * 68


def test_mpeg2_mono_frames_are_measured_with_their_own_tables(tmp_path):
    path = tmp_path / "spot.mp3"
    info = b"\x00" * 9 + b"Info" + b"\x00\x00\x00\x01" + b"\x00" * 4 + b"Lavc61.19" + b"\x00" * 12
    info += ((100 << 12) | 200).to_bytes(3, "big")
    path.write_bytes(_mpeg2_mono_frame(info) + _mpeg2_mono_frame() * 10)
    assert spoken_assets._mp3_duration_seconds(path) == pytest.approx((10 * 576 - 300) / 22050)


def test_mpeg25_frames_are_measured_with_their_own_tables(tmp_path):
    path = tmp_path / "spot.mp3"
    path.write_bytes(_mpeg25_mono_frame() * 3)
    assert spoken_assets._mp3_duration_seconds(path) == pytest.approx(3 * 576 / 8000)


def test_frames_that_change_version_or_sample_rate_are_refused(tmp_path):
    path = tmp_path / "spot.mp3"
    path.write_bytes(_mpeg2_mono_frame() * 10 + _mpeg25_mono_frame() * 3)
    with pytest.raises(ValueError, match="changes MPEG version or sample rate"):
        spoken_assets._mp3_duration_seconds(path)


def test_audio_that_happens_to_spell_tag_is_not_cut_as_id3v1(tmp_path):
    audio = bytearray(_mp3_bytes(30.0))
    audio[-128:-125] = b"TAG"
    path = tmp_path / "spot.mp3"
    path.write_bytes(bytes(audio))
    assert spoken_assets._mp3_duration_seconds(path) == pytest.approx(
        round(30.0 / _MP3_FRAME_SECONDS) * _MP3_FRAME_SECONDS
    )


def test_every_xing_field_shifts_the_lame_tag_it_precedes(tmp_path):
    info = b"\x00" * 32 + b"Xing" + b"\x00\x00\x00\x0f" + b"\x00" * (4 + 4 + 100 + 4)
    info += b"LAME3.100" + b"\x00" * 12 + ((576 << 12) | 1000).to_bytes(3, "big")
    path = tmp_path / "spot.mp3"
    path.write_bytes(_mp3_frame(info) + _mp3_bytes(30.0))
    expected = (round(30.0 / _MP3_FRAME_SECONDS) * 1152 - 1576) / 44100
    assert spoken_assets._mp3_duration_seconds(path) == pytest.approx(expected)


def test_an_id3v2_footer_is_skipped(tmp_path):
    path = tmp_path / "spot.mp3"
    path.write_bytes(b"ID3\x04\x00\x10\x00\x00\x00\x02" + b"\x00" * 2 + b"3DI" + b"\x00" * 7 + _mp3_bytes(30.0))
    assert spoken_assets._mp3_duration_seconds(path) == pytest.approx(
        round(30.0 / _MP3_FRAME_SECONDS) * _MP3_FRAME_SECONDS
    )


@pytest.mark.parametrize("tail", [b"\xff", b"\xff\xfb", b"\xff\xfb\x90"])
def test_a_partial_frame_header_at_the_end_is_refused(tmp_path, tail):
    path = tmp_path / "spot.mp3"
    path.write_bytes(_mp3_bytes(30.0) + tail)
    with pytest.raises(ValueError, match="trailing bytes"):
        spoken_assets._mp3_duration_seconds(path)


def test_a_trim_longer_than_the_audio_leaves_no_audio(tmp_path):
    path = tmp_path / "spot.mp3"
    path.write_bytes(_mp3_bytes(0.05, gapless=(4000, 4000)))
    with pytest.raises(ValueError, match="no audio frames"):
        spoken_assets._mp3_duration_seconds(path)


def test_a_crc_flagged_info_frame_is_read_where_lame_writes_it(tmp_path):
    """LAME ``-p`` sets the CRC bit on its Info frame but writes no CRC gap.

    The tag sits right after the header and side information, and ffmpeg reads
    it there too. Shifting two bytes for the CRC misses the tag, counts the
    Info frame as audio and skips the gapless trim, so a real encode reads
    about 0.07s long.
    """
    info = b"\x00" * 32 + b"Info" + b"\x00\x00\x00\x01" + b"\x00" * 4
    info += b"LAME3.100" + b"\x00" * 12 + ((576 << 12) | 1000).to_bytes(3, "big")
    crc_flagged_frame = b"\xff\xfa\x90\x00" + info.ljust(413, b"\x00")
    path = tmp_path / "spot.mp3"
    path.write_bytes(crc_flagged_frame + _mp3_bytes(30.0))
    expected = (round(30.0 / _MP3_FRAME_SECONDS) * 1152 - 1576) / 44100
    assert spoken_assets._mp3_duration_seconds(path) == pytest.approx(expected)

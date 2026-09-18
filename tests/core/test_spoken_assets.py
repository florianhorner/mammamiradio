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


def _ad_entry(path, payload, *, mode="normal", language="en", duration=30.0, **overrides):
    entry = _entry(path, payload, language=language, transcript="A finished thirty-second spot for a fictional brand.")
    entry["mode"] = mode
    entry["duration_seconds"] = duration
    entry.update(overrides)
    return entry


def _ad_root(tmp_path, *, duration=30.0, **overrides):
    (tmp_path / "ads").mkdir()
    payload = b"packaged-ad"
    (tmp_path / "ads" / "spot.mp3").write_bytes(payload)
    _write_manifest(tmp_path, [_ad_entry("ads/spot.mp3", payload, duration=duration, **overrides)])
    return tmp_path


def test_a_packaged_ad_of_ordinary_length_is_admitted(tmp_path):
    root = _ad_root(tmp_path)
    assert validate_spoken_asset_manifest(assets_root=root) == []
    entries = approved_spoken_asset_entries("ads", assets_root=root)
    assert [entry.relative_path for entry in entries] == ["ads/spot.mp3"]
    assert entries[0].duration_seconds == 30.0


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
    payload = b"packaged-ad"
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


def test_an_unrepresentable_duration_fails_closed_instead_of_raising(tmp_path):
    """JSON integers are unbounded and ``float()`` raises on one too large.

    The same parser answers the dead-air rescue ladder's question about which
    packaged audio it may use, and that question must never raise.
    """
    (tmp_path / "recovery").mkdir()
    payload = b"continuity"
    (tmp_path / "recovery" / "continuity_1.mp3").write_bytes(payload)
    entry = json.dumps(_entry("recovery/continuity_1.mp3", payload, language="it"))
    huge = entry[:-1] + ', "duration_seconds": 1' + "0" * 400 + "}"
    (tmp_path / "spoken_assets.json").write_text('{"schema_version": 1, "assets": [' + huge + "]}", encoding="utf-8")

    errors = validate_spoken_asset_manifest(assets_root=tmp_path)
    assert any("non-negative finite number" in error for error in errors), errors
    assert is_approved_packaged_audio_asset(tmp_path / "recovery" / "continuity_1.mp3", assets_root=tmp_path) is False
    assert approved_spoken_asset_entries("recovery", assets_root=tmp_path) == []

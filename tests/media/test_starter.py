from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mammamiradio.media.starter import (
    CATALOG_PATH,
    STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS,
    STARTER_MINIMUM_DURATION_SECONDS,
    STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS,
    StarterCatalogError,
    StarterCatalogExhaustedError,
    StarterShuffleBag,
    load_starter_catalog,
    load_starter_rotation_tracks,
    load_starter_tracks,
    resolve_starter_track,
    starter_source,
    starter_track,
    verify_starter_entry,
)

LOCKED_TRACKS = [
    ("USUAN1400037", "Carefree", 205),
    ("USUAN1700019", "Miami Viceroy", 268),
    ("USUAN1600018", "Floating Cities", 184),
    ("USUAN1500001", "Allada", 223),
    ("USUAN1400054", "Life of Riley", 235),
    ("USUAN1900056", "Night in Venice", 218),
    ("USUAN1100106", "Pride", 349),
    ("USUAN1600012", "Casa Bossa Nova", 242),
    ("USUAN1100844", "Cipher", 231),
    ("USUAN1500025", "Daily Beetle", 317),
    ("USUAN1300012", "Local Forecast - Elevator", 189),
    ("USUAN1100843", "Wallpaper", 215),
]


def _approved_manifest(root: Path, *, count: int = 3) -> Path:
    tracks = []
    for index in range(count):
        isrc = f"USUAN9900{index:03d}"
        title = f"Fixture {index}"
        relative = f"tracks/{isrc.lower()}.mp3"
        payload = f"fixture-mp3-{index}".encode()
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        tracks.append(
            {
                "isrc": isrc,
                "title": title,
                "artist": "Kevin MacLeod",
                "expected_duration_seconds": 100,
                "official_piece_url": f"https://incompetech.com/music/royalty-free/index.html?isrc={isrc}",
                "license": {
                    "id": "CC-BY-4.0",
                    "url": "https://creativecommons.org/licenses/by/4.0/",
                },
                "attribution": f'"{title}" Kevin MacLeod (incompetech.com), licensed under CC BY 4.0.',
                "source": {
                    "url": f"https://incompetech.com/music/royalty-free/mp3-royaltyfree/{title}.mp3",
                    "filename": f"{title}.mp3",
                    "sha256": "a" * 64,
                    "evidence_url": f"https://incompetech.com/music/royalty-free/index.html?isrc={isrc}",
                },
                "derivative": {
                    "path": relative,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                    "codec": "mp3",
                    "sample_rate_hz": 48000,
                    "channels": 2,
                    "bitrate_kbps": 192,
                    "duration_seconds": 100.125 + index,
                    "integrated_loudness_lufs": -16.0,
                },
                "modification_notice": "Normalized for broadcast.",
                "evidence": {
                    "source_receipt": "proof/media/source.json",
                    "audition_receipt": f"proof/media/{isrc.lower()}-approval.json",
                },
                "approval": {"status": "approved", "reviewed_at": "2026-07-16T00:00:00Z"},
            }
        )
    manifest = {
        "schema_version": "1",
        "catalog_id": "starter-v1",
        "release_requirements": {
            "exact_track_count": count,
            "minimum_duration_seconds": count * 100,
            "maximum_payload_bytes": 1_000_000,
            "audio": {
                "codec": "mp3",
                "sample_rate_hz": 48000,
                "channels": 2,
                "bitrate_kbps": 192,
            },
        },
        "tracks": tracks,
    }
    path = root / "catalog.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_canonical_manifest_locks_exact_pending_catalog() -> None:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    assert data["schema_version"] == "1"
    assert data["catalog_id"] == "starter-v1"
    assert [(row["isrc"], row["title"], row["expected_duration_seconds"]) for row in data["tracks"]] == LOCKED_TRACKS
    assert sum(row["expected_duration_seconds"] for row in data["tracks"]) == 2876
    assert STARTER_MINIMUM_DURATION_SECONDS == 2700
    assert (STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS, STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS) == (-17.0, -15.0)
    artists = {row["isrc"]: row["artist"] for row in data["tracks"]}
    assert artists["USUAN1500025"] == "Kevin MacLeod feat. Brett Van Donsel"
    assert all(artist == "Kevin MacLeod" for isrc, artist in artists.items() if isrc != "USUAN1500025")
    daily_beetle = next(row for row in data["tracks"] if row["isrc"] == "USUAN1500025")
    assert daily_beetle["credits"] == [
        {"name": "Kevin MacLeod", "role": "composer and copyright holder"},
        {
            "name": "Brett Van Donsel",
            "role": "featured guitar",
            "evidence_url": "https://incompetech.com/wordpress/2015/04/daily-beetle/",
        },
    ]
    assert all(row["license"]["id"] == "CC-BY-4.0" for row in data["tracks"])
    assert all(len(row["source"]["sha256"]) == 64 for row in data["tracks"])
    assert all(row["approval"]["status"] == "pending_full_audition" for row in data["tracks"])
    assert all(row["derivative"] is None for row in data["tracks"])


def test_pending_catalog_never_becomes_playable_placeholder() -> None:
    catalog = load_starter_catalog(require_complete=False)

    assert catalog.declared_count == 12
    assert catalog.expected_duration_seconds == 2876
    assert catalog.entries == ()
    assert load_starter_tracks(require_complete=False) == []
    with pytest.raises(StarterCatalogError, match="0 approved derivatives"):
        load_starter_catalog()


def test_approved_catalog_loads_and_maps_to_track(tmp_path: Path) -> None:
    catalog = load_starter_catalog(_approved_manifest(tmp_path, count=1), verify_all=True)
    entry = catalog.entries[0]

    track = starter_track(entry)

    assert track.source == "starter"
    assert track.local_path == entry.path
    assert track.provider_track_id == entry.isrc
    assert track.duration_ms == 100125
    assert track.attribution is not None
    assert track.attribution.provider == "incompetech"
    assert track.attribution.basis == "bundled_manifest"
    assert track.attribution.modified is True
    assert resolve_starter_track(track, catalog_path=tmp_path / "catalog.json") == entry


def test_resolve_starter_track_rejects_mutated_runtime_facts(tmp_path: Path) -> None:
    manifest_path = _approved_manifest(tmp_path, count=1)
    entry = load_starter_catalog(manifest_path).entries[0]
    track = starter_track(entry)
    track.title = "Substituted title"

    with pytest.raises(StarterCatalogError, match="runtime facts do not match"):
        resolve_starter_track(track, catalog_path=manifest_path)


def test_resolve_starter_track_rejects_unapproved_source(tmp_path: Path) -> None:
    manifest_path = _approved_manifest(tmp_path, count=1)
    track = starter_track(load_starter_catalog(manifest_path).entries[0])
    track.source = "local"

    with pytest.raises(StarterCatalogError, match="not a manifested starter"):
        resolve_starter_track(track, catalog_path=manifest_path)


def test_resolve_starter_track_rejects_unknown_isrc(tmp_path: Path) -> None:
    manifest_path = _approved_manifest(tmp_path, count=1)
    track = starter_track(load_starter_catalog(manifest_path).entries[0])
    track.provider_track_id = "USUAN0000000"

    with pytest.raises(StarterCatalogError, match="not uniquely approved"):
        resolve_starter_track(track, catalog_path=manifest_path)


def test_starter_source_uses_starter_not_demo_name() -> None:
    source = starter_source(12)

    assert source.kind == "starter"
    assert source.source_id == "starter-v1"
    assert source.track_count == 12
    assert "demo" not in source.label.casefold()
    assert source.url == "starter://catalog/v1"


def test_shuffle_bag_is_digest_deterministic_and_cycles_without_repeat(tmp_path: Path) -> None:
    catalog = load_starter_catalog(_approved_manifest(tmp_path, count=4), verify_all=True)
    first = StarterShuffleBag(catalog)
    second = StarterShuffleBag(catalog)

    first_cycle = [first.next_track().isrc for _ in range(4)]
    second_cycle = [second.next_track().isrc for _ in range(4)]
    next_track = first.next_track().isrc

    assert first_cycle == second_cycle
    assert len(set(first_cycle)) == 4
    assert next_track != first_cycle[-1]
    assert first.manifest_digest == catalog.manifest_digest


def test_rotation_helper_drains_one_verified_cycle(tmp_path: Path) -> None:
    manifest_path = _approved_manifest(tmp_path, count=4)

    first = load_starter_rotation_tracks(seed="station", catalog_path=manifest_path)
    second = load_starter_rotation_tracks(seed="station", catalog_path=manifest_path)

    assert [track.provider_track_id for track in first] == [track.provider_track_id for track in second]
    assert len({track.provider_track_id for track in first}) == 4
    assert all(track.source == "starter" and track.local_path is not None for track in first)


def test_rotation_helper_never_fills_a_corrupt_cycle_with_duplicates(tmp_path: Path) -> None:
    manifest_path = _approved_manifest(tmp_path, count=3)
    catalog = load_starter_catalog(manifest_path)
    catalog.entries[0].path.write_bytes(b"corrupt")

    with pytest.raises(StarterCatalogExhaustedError, match=r"valid tracks|mismatch"):
        load_starter_rotation_tracks(seed="station", catalog_path=manifest_path)


def test_shuffle_bag_rotates_an_identical_cycle_boundary(tmp_path: Path, monkeypatch) -> None:
    catalog = load_starter_catalog(_approved_manifest(tmp_path, count=2))
    bag = StarterShuffleBag(catalog)
    bag._last_index = 1
    monkeypatch.setattr(bag._random, "shuffle", lambda values: None)

    bag._refill()

    assert bag._remaining == [1, 0]


def test_shuffle_bag_consumes_corrupt_entry_before_failing_cycle(tmp_path: Path) -> None:
    catalog = load_starter_catalog(_approved_manifest(tmp_path, count=2))
    bag = StarterShuffleBag(catalog, seed="fixed")
    corrupt = catalog.entries[0]
    corrupt.path.write_bytes(b"changed")

    first = bag.next_track()
    assert first != corrupt
    with pytest.raises(StarterCatalogExhaustedError, match=r"hash mismatch|size mismatch"):
        bag.next_track()


def test_empty_catalog_cannot_create_shuffle_bag(tmp_path: Path) -> None:
    """A catalog with nothing approved must refuse to hand out audio.

    Built from a synthetic pending manifest rather than the shipped one: this
    invariant has to hold whatever state the real crate happens to be in, and
    tying it to "the shipped catalog is empty" made it silently stop testing
    anything the moment the crate was filled.
    """
    manifest_path = _approved_manifest(tmp_path, count=1)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    for row in data["tracks"]:
        row["approval"] = {"status": "pending_full_audition"}
        row["derivative"] = None
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    catalog = load_starter_catalog(manifest_path, require_complete=False)

    assert catalog.entries == ()
    with pytest.raises(StarterCatalogExhaustedError, match="no approved"):
        StarterShuffleBag(catalog)


def test_verify_entry_rejects_size_and_hash_changes(tmp_path: Path) -> None:
    catalog = load_starter_catalog(_approved_manifest(tmp_path, count=1))
    entry = catalog.entries[0]
    entry.path.write_bytes(b"x")
    with pytest.raises(StarterCatalogError, match="size mismatch"):
        verify_starter_entry(entry)

    entry.path.write_bytes(b"z" * entry.byte_size)
    with pytest.raises(StarterCatalogError, match="hash mismatch"):
        verify_starter_entry(entry)

    entry.path.unlink()
    with pytest.raises(StarterCatalogError, match="asset is missing"):
        verify_starter_entry(entry)


def test_verify_entry_rejects_symlink_swap_after_catalog_load(tmp_path: Path) -> None:
    entry = load_starter_catalog(_approved_manifest(tmp_path, count=1)).entries[0]
    outside = tmp_path / "outside-copy.mp3"
    outside.write_bytes(entry.path.read_bytes())
    entry.path.unlink()
    entry.path.symlink_to(outside)

    with pytest.raises(StarterCatalogError, match="may not be a symlink"):
        verify_starter_entry(entry)


def test_loader_rejects_symlinked_derivative(tmp_path: Path) -> None:
    manifest_path = _approved_manifest(tmp_path, count=1)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    relative = data["tracks"][0]["derivative"]["path"]
    declared = tmp_path / relative
    target = tmp_path / "outside.mp3"
    target.write_bytes(declared.read_bytes())
    declared.unlink()
    declared.symlink_to(target)

    with pytest.raises(StarterCatalogError, match="symlink"):
        load_starter_catalog(manifest_path)


@pytest.mark.parametrize("payload", ["{", "[]"])
def test_loader_rejects_unreadable_or_non_object_manifest(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(StarterCatalogError, match=r"unreadable|root must be an object"):
        load_starter_catalog(path)


def test_loader_rejects_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(StarterCatalogError, match="unreadable"):
        load_starter_catalog(tmp_path / "missing.json")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update(schema_version="2"), "schema_version"),
        (lambda data: data.update(catalog_id="other"), "catalog_id"),
        (lambda data: data.update(tracks={}), "tracks must be a list"),
        (
            lambda data: data["tracks"][0]["derivative"].update(path="../escape.mp3"),
            "unsafe starter asset path",
        ),
        (
            lambda data: data["tracks"].append(dict(data["tracks"][0])),
            "duplicate ISRC",
        ),
        (
            lambda data: data["tracks"][0]["license"].update(id="CC-BY-NC"),
            "must be attribution-only CC BY 3.0 or 4.0",
        ),
        (lambda data: data.update(release_requirements=[]), "release_requirements must be an object"),
        (
            lambda data: data["release_requirements"].update(exact_track_count=0),
            "exact_track_count must be positive",
        ),
        (
            lambda data: data["release_requirements"].update(minimum_duration_seconds=0),
            "minimum_duration_seconds must be positive",
        ),
        (
            lambda data: data["release_requirements"].update(maximum_payload_bytes=0),
            "maximum_payload_bytes must be positive",
        ),
        (lambda data: data["tracks"][0].update(title=""), "title must be a non-empty string"),
        (
            lambda data: data["tracks"][0].update(expected_duration_seconds=0),
            "expected_duration_seconds must be positive",
        ),
        (lambda data: data["tracks"][0].update(approval={"status": "maybe"}), "approval.status is invalid"),
        (
            lambda data: data["tracks"][0]["source"].update(sha256="short"),
            "source.sha256 must be SHA-256",
        ),
        (
            lambda data: data["tracks"][0]["derivative"].update(sha256="short"),
            "derivative.sha256 must be SHA-256",
        ),
        (
            lambda data: data["tracks"][0]["derivative"].update(bytes=0),
            "derivative.bytes must be positive",
        ),
        (
            lambda data: data["tracks"][0]["derivative"].update(duration_seconds=0),
            "derivative.duration_seconds must be positive",
        ),
        (
            lambda data: data["tracks"][0]["derivative"].update(codec="aac"),
            "must be 48 kHz MP3",
        ),
        (
            lambda data: data["tracks"][0]["derivative"].update(channels=1),
            "must be stereo at 192 kbps",
        ),
        (
            lambda data: data["tracks"][0]["derivative"].update(path="tracks"),
            "not a regular file",
        ),
    ],
)
def test_loader_rejects_manifest_contract_violations(tmp_path: Path, mutation, message: str) -> None:
    manifest_path = _approved_manifest(tmp_path, count=1)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(data)
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(StarterCatalogError, match=message):
        load_starter_catalog(manifest_path)


def test_loader_reports_duration_and_payload_release_failures(tmp_path: Path) -> None:
    manifest_path = _approved_manifest(tmp_path, count=1)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["release_requirements"]["minimum_duration_seconds"] = 101
    data["release_requirements"]["maximum_payload_bytes"] = 1
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(StarterCatalogError, match=r"below|exceeds"):
        load_starter_catalog(manifest_path)


@pytest.mark.parametrize(
    ("license_id", "license_url"),
    [
        ("CC-BY-4.0", "https://creativecommons.org/licenses/by/4.0/"),
        ("CC-BY-3.0", "https://creativecommons.org/licenses/by/3.0/"),
        # Jamendo reports http:// and sometimes omits the trailing slash, so both
        # normalizations have to hold at the loader gate too — not just in the
        # acquire tool, which is where they were previously only covered.
        ("CC-BY-3.0", "http://creativecommons.org/licenses/by/3.0/"),
        ("CC-BY-3.0", "http://creativecommons.org/licenses/by/3.0"),
    ],
)
def test_loader_accepts_attribution_only_in_every_reported_spelling(
    tmp_path: Path, license_id: str, license_url: str
) -> None:
    manifest_path = _approved_manifest(tmp_path, count=1)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["tracks"][0]["license"] = {"id": license_id, "url": license_url}
    if license_id != "CC-BY-4.0":
        data["tracks"][0]["provider"] = "jamendo"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    catalog = load_starter_catalog(manifest_path)

    assert catalog.entries[0].license_id == license_id


def test_loader_rejects_an_unknown_provider(tmp_path: Path) -> None:
    """An unrecognised provider must fail loudly, not lose the credit quietly.

    `starter_track` feeds the provider straight into `MediaAttribution`, and the
    public attribution gate branches on it — an unknown value falls through and
    returns None, stripping the CC BY credit. That is the licence breach this
    catalog already shipped once, so the value is checked at load rather than
    trusted downstream.
    """
    manifest_path = _approved_manifest(tmp_path, count=1)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["tracks"][0]["provider"] = "soundcloud"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(StarterCatalogError, match="not a known source"):
        load_starter_catalog(manifest_path)

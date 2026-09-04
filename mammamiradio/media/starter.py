"""Validated access to the packaged, rights-reviewed starter catalog.

The catalog manifest is the only runtime authority for bundled music.  Entries
that have not completed the explicit approval workflow are never returned to
callers, even when an audio file happens to be present beside the manifest.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mammamiradio.core.models import MediaAttribution, PlaylistSource, Track

STARTER_ROOT = Path(__file__).resolve().parents[1] / "assets" / "starter"
CATALOG_PATH = STARTER_ROOT / "catalog.json"
STARTER_MINIMUM_DURATION_SECONDS = 45 * 60
STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS = -17.0
STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS = -15.0


class StarterCatalogError(ValueError):
    """The starter manifest or one of its declared files is invalid."""


class StarterCatalogExhaustedError(RuntimeError):
    """No validated starter track remains in the current catalog cycle."""


@dataclass(frozen=True, slots=True)
class StarterEntry:
    """One approved derivative from the canonical starter manifest."""

    isrc: str
    provider: str
    title: str
    artist: str
    duration_seconds: float
    path: Path
    sha256: str
    byte_size: int
    official_piece_url: str
    license_id: str
    license_url: str
    attribution: str
    modification_notice: str
    evidence_basis: str


@dataclass(frozen=True, slots=True)
class StarterCatalog:
    """Approved starter entries plus the digest that defines their cycle."""

    entries: tuple[StarterEntry, ...]
    manifest_digest: str
    declared_count: int
    expected_duration_seconds: int


def _canonical_digest(data: Any) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StarterCatalogError(f"starter manifest is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise StarterCatalogError("starter manifest root must be an object")
    if data.get("schema_version") != "1":
        raise StarterCatalogError("starter manifest schema_version must be '1'")
    if data.get("catalog_id") != "starter-v1":
        raise StarterCatalogError("starter manifest catalog_id must be 'starter-v1'")
    if not isinstance(data.get("tracks"), list):
        raise StarterCatalogError("starter manifest tracks must be a list")
    return data


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StarterCatalogError(f"{field} must be a non-empty string")
    return value.strip()


def _required_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StarterCatalogError(f"{field} must be an object")
    return value


def _require_attribution_only_license(license_id: str, license_url: str, provider: str, prefix: str) -> None:
    """Reject anything the bundle is not allowed to carry.

    Bundled music must be attribution-only. Two clauses are fatal and neither is
    a matter of taste:

    * **NoDerivatives** — every bundled track is normalized to a fixed loudness
      and re-encoded, which is a derivative work. ND forbids distributing one, so
      an ND track can never ship no matter how good it sounds.
    * **NonCommercial** — the package is redistributed by every operator who
      installs it, and we will not pin that on an NC boundary.

    ShareAlike is refused too: it is viral, and the bundle sits inside a project
    that does not carry a copyleft media obligation.

    Versions 3.0 and 4.0 are both accepted. They are materially equivalent for
    bundle-and-attribute — 4.0 mainly improves international wording, adds
    database rights and a cure period. Jamendo publishes no 4.0 at all, so
    requiring it would exclude that catalog entirely. Older versions (2.x) stay
    out: they are rarer, and their wording has drifted furthest from 4.0.

    Matching is exact equality against an allowlist, after normalizing only the
    scheme and a trailing slash. Deliberately not a substring or path check: a
    substring test would let ``licenses/by-nc/`` satisfy ``licenses/by``, which
    is the exact hole this gate exists to close. An unrecognised variant fails
    closed.
    """
    by_id = {
        "CC-BY-3.0": "https://creativecommons.org/licenses/by/3.0/",
        "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
    }
    # Scoped per provider, and it has to match what the public attribution gate
    # in core.models will accept. If this admits a row that gate refuses, the
    # track still plays but ships with no credit at all, which is the silent
    # licence breach rather than a loud failure.
    allowed_ids = {"incompetech": {"CC-BY-4.0"}, "jamendo": {"CC-BY-3.0", "CC-BY-4.0"}}[provider]
    expected_url = by_id.get(license_id) if license_id in allowed_ids else None
    if expected_url is None:
        raise StarterCatalogError(
            f"{prefix}.license.id must be attribution-only "
            f"{' or '.join(sorted(allowed_ids))} for {provider}, got {license_id!r}"
        )
    # Compare scheme-insensitively: Creative Commons serves both, and Jamendo
    # reports http:// while the canonical form is https://.
    normalized = license_url.replace("http://", "https://", 1)
    if not normalized.endswith("/"):
        normalized += "/"
    if normalized != expected_url:
        raise StarterCatalogError(f"{prefix}.license.url does not match {license_id}: {license_url!r}")


def _safe_asset_path(root: Path, relative: str) -> Path:
    declared = Path(relative)
    if declared.is_absolute() or not declared.parts or ".." in declared.parts:
        raise StarterCatalogError(f"unsafe starter asset path: {relative!r}")
    candidate = root / declared
    cursor = root
    for part in declared.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise StarterCatalogError(f"starter asset may not be a symlink: {relative}")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        # A symlink cycle in the root's own ancestry is reported differently
        # by version: RuntimeError on Python 3.12 and earlier, OSError(ELOOP)
        # on 3.13 and later. ValueError is a null byte. Both interpreter
        # families ship here, and either way the asset is unusable, which is
        # what this error says.
        raise StarterCatalogError(f"starter asset is missing: {relative}") from exc
    if resolved_root not in resolved.parents:
        raise StarterCatalogError(f"starter asset escapes the catalog root: {relative}")
    if not resolved.is_file():
        raise StarterCatalogError(f"starter asset is not a regular file: {relative}")
    return resolved


def verify_starter_entry(entry: StarterEntry) -> None:
    """Synchronously verify the selected derivative before queue admission."""

    if entry.path.is_symlink():
        raise StarterCatalogError(f"starter asset may not be a symlink: {entry.path.name}")
    try:
        stat = entry.path.stat()
    except OSError as exc:
        raise StarterCatalogError(f"starter asset is missing: {entry.path.name}") from exc
    if stat.st_size != entry.byte_size:
        raise StarterCatalogError(f"starter asset size mismatch: {entry.path.name}")
    digest = hashlib.sha256()
    try:
        with entry.path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StarterCatalogError(f"starter asset is unreadable: {entry.path.name}") from exc
    if digest.hexdigest() != entry.sha256:
        raise StarterCatalogError(f"starter asset hash mismatch: {entry.path.name}")


def load_starter_catalog(
    catalog_path: Path = CATALOG_PATH,
    *,
    require_complete: bool = True,
    verify_all: bool = False,
) -> StarterCatalog:
    """Load approved entries, rejecting pending or invalid release catalogs.

    ``verify_all=False`` keeps the cold path cheap.  ``StarterShuffleBag``
    verifies only the selected file synchronously before returning it.
    """

    data = _read_manifest(catalog_path)
    root = catalog_path.parent
    requirements = _required_dict(data.get("release_requirements"), "release_requirements")
    exact_count = requirements.get("exact_track_count")
    minimum_seconds = requirements.get("minimum_duration_seconds")
    maximum_bytes = requirements.get("maximum_payload_bytes")
    if not isinstance(exact_count, int) or exact_count < 1:
        raise StarterCatalogError("release_requirements.exact_track_count must be positive")
    if not isinstance(minimum_seconds, int) or minimum_seconds < 1:
        raise StarterCatalogError("release_requirements.minimum_duration_seconds must be positive")
    if not isinstance(maximum_bytes, int) or maximum_bytes < 1:
        raise StarterCatalogError("release_requirements.maximum_payload_bytes must be positive")

    tracks = data["tracks"]
    seen_isrc: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    expected_duration = 0
    approved: list[StarterEntry] = []
    errors: list[str] = []

    for index, raw in enumerate(tracks):
        prefix = f"tracks[{index}]"
        try:
            row = _required_dict(raw, prefix)
            isrc = _required_text(row.get("isrc"), f"{prefix}.isrc")
            title = _required_text(row.get("title"), f"{prefix}.title")
            artist = _required_text(row.get("artist"), f"{prefix}.artist")
            if isrc in seen_isrc:
                raise StarterCatalogError(f"duplicate ISRC: {isrc}")
            title_key = (artist.casefold(), title.casefold())
            if title_key in seen_titles:
                raise StarterCatalogError(f"duplicate artist/title: {artist} / {title}")
            seen_isrc.add(isrc)
            seen_titles.add(title_key)
            declared_duration = row.get("expected_duration_seconds")
            if not isinstance(declared_duration, int) or declared_duration < 1:
                raise StarterCatalogError(f"{prefix}.expected_duration_seconds must be positive")
            expected_duration += declared_duration

            license_data = _required_dict(row.get("license"), f"{prefix}.license")
            license_id = _required_text(license_data.get("id"), f"{prefix}.license.id")
            license_url = _required_text(license_data.get("url"), f"{prefix}.license.url")
            source = _required_dict(row.get("source"), f"{prefix}.source")
            _required_text(source.get("url"), f"{prefix}.source.url")
            _required_text(source.get("filename"), f"{prefix}.source.filename")
            _required_text(source.get("evidence_url"), f"{prefix}.source.evidence_url")
            _required_text(row.get("official_piece_url"), f"{prefix}.official_piece_url")
            _required_text(row.get("attribution"), f"{prefix}.attribution")

            approval = _required_dict(row.get("approval"), f"{prefix}.approval")
            status = approval.get("status")
            if status not in {"pending_full_audition", "approved"}:
                raise StarterCatalogError(f"{prefix}.approval.status is invalid")
            if status != "approved":
                continue

            # Absent means Incompetech: rows predating the second provider omit
            # the field. Naming the real provider matters beyond bookkeeping —
            # it is what lets the attribution gate accept the row, and a CC BY
            # track whose credit is dropped is a licence breach, not a cosmetic
            # gap.
            provider = row.get("provider", "incompetech")
            if provider not in {"incompetech", "jamendo"}:
                raise StarterCatalogError(f"{prefix}.provider is not a known source: {provider!r}")
            _require_attribution_only_license(license_id, license_url, str(provider), prefix)
            source_sha = _required_text(source.get("sha256"), f"{prefix}.source.sha256")
            if len(source_sha) != 64:
                raise StarterCatalogError(f"{prefix}.source.sha256 must be SHA-256")
            derivative = _required_dict(row.get("derivative"), f"{prefix}.derivative")
            relative_path = _required_text(derivative.get("path"), f"{prefix}.derivative.path")
            derivative_sha = _required_text(derivative.get("sha256"), f"{prefix}.derivative.sha256")
            byte_size = derivative.get("bytes")
            duration = derivative.get("duration_seconds")
            if len(derivative_sha) != 64:
                raise StarterCatalogError(f"{prefix}.derivative.sha256 must be SHA-256")
            if not isinstance(byte_size, int) or byte_size < 1:
                raise StarterCatalogError(f"{prefix}.derivative.bytes must be positive")
            if not isinstance(duration, int | float) or duration <= 0:
                raise StarterCatalogError(f"{prefix}.derivative.duration_seconds must be positive")
            if derivative.get("codec") != "mp3" or derivative.get("sample_rate_hz") != 48000:
                raise StarterCatalogError(f"{prefix}.derivative must be 48 kHz MP3")
            if derivative.get("channels") != 2 or derivative.get("bitrate_kbps") != 192:
                raise StarterCatalogError(f"{prefix}.derivative must be stereo at 192 kbps")
            evidence = _required_dict(row.get("evidence"), f"{prefix}.evidence")
            source_receipt = _required_text(evidence.get("source_receipt"), f"{prefix}.evidence.source_receipt")
            audition_receipt = _required_text(evidence.get("audition_receipt"), f"{prefix}.evidence.audition_receipt")
            modification = _required_text(row.get("modification_notice"), f"{prefix}.modification_notice")
            asset_path = _safe_asset_path(root, relative_path)
            entry = StarterEntry(
                isrc=isrc,
                provider=str(provider),
                title=title,
                artist=artist,
                duration_seconds=float(duration),
                path=asset_path,
                sha256=derivative_sha,
                byte_size=byte_size,
                official_piece_url=_required_text(row.get("official_piece_url"), f"{prefix}.official_piece_url"),
                license_id=license_id,
                license_url=license_url,
                attribution=_required_text(row.get("attribution"), f"{prefix}.attribution"),
                modification_notice=modification,
                evidence_basis=f"{source_receipt}; {audition_receipt}",
            )
            if verify_all:
                verify_starter_entry(entry)
            approved.append(entry)
        except StarterCatalogError as exc:
            errors.append(str(exc))

    if len(tracks) != exact_count:
        errors.append(f"catalog declares {len(tracks)} tracks; release requires exactly {exact_count}")
    if expected_duration < minimum_seconds:
        errors.append(f"catalog declares {expected_duration}s; release requires at least {minimum_seconds}s")
    if require_complete and len(approved) != exact_count:
        errors.append(f"catalog has {len(approved)} approved derivatives; release requires {exact_count}")
    if sum(entry.byte_size for entry in approved) > maximum_bytes:
        errors.append(f"approved starter payload exceeds {maximum_bytes} bytes")
    if errors:
        raise StarterCatalogError("; ".join(errors))

    return StarterCatalog(
        entries=tuple(approved),
        manifest_digest=_canonical_digest(data),
        declared_count=len(tracks),
        expected_duration_seconds=expected_duration,
    )


class StarterShuffleBag:
    """Digest-pinned cycle that returns every approved entry before a repeat."""

    def __init__(self, catalog: StarterCatalog, *, seed: str | int | None = None) -> None:
        if not catalog.entries:
            raise StarterCatalogExhaustedError("starter catalog has no approved derivatives")
        self._catalog = catalog
        material = catalog.manifest_digest if seed is None else str(seed)
        digest = hashlib.sha256(material.encode()).digest()
        self._random = random.Random(int.from_bytes(digest[:16]))
        self._remaining: list[int] = []
        self._last_index: int | None = None
        self._cycle_started = False
        self._cycle_returned = 0

    @property
    def manifest_digest(self) -> str:
        return self._catalog.manifest_digest

    def _refill(self) -> None:
        self._remaining = list(range(len(self._catalog.entries)))
        self._random.shuffle(self._remaining)
        if len(self._remaining) > 1 and self._last_index == self._remaining[-1]:
            self._remaining[0], self._remaining[-1] = self._remaining[-1], self._remaining[0]
        self._cycle_started = True
        self._cycle_returned = 0

    def next_track(self) -> StarterEntry:
        """Return the next hash-valid entry, consuming invalid entries in-cycle."""

        if not self._remaining:
            if self._cycle_started and self._cycle_returned < len(self._catalog.entries):
                raise StarterCatalogExhaustedError(
                    f"starter catalog cycle returned {self._cycle_returned}/{len(self._catalog.entries)} valid tracks"
                )
            self._refill()
        attempts = len(self._remaining)
        failures: list[str] = []
        for _ in range(attempts):
            index = self._remaining.pop()
            entry = self._catalog.entries[index]
            try:
                verify_starter_entry(entry)
            except StarterCatalogError as exc:
                failures.append(str(exc))
                continue
            self._last_index = index
            self._cycle_returned += 1
            return entry
        detail = "; ".join(failures) if failures else "no entries remained"
        raise StarterCatalogExhaustedError(f"starter catalog cycle has no valid track: {detail}")


def starter_track(entry: StarterEntry) -> Track:
    """Convert one approved manifest entry to the existing playback model."""

    return Track(
        title=entry.title,
        artist=entry.artist,
        duration_ms=round(entry.duration_seconds * 1000),
        spotify_id=f"starter_{entry.isrc.lower()}",
        local_path=entry.path,
        source="starter",
        provider_track_id=entry.isrc,
        attribution=MediaAttribution(
            provider=entry.provider,
            license_id=entry.license_id,
            license_url=entry.license_url,
            source_url=entry.official_piece_url,
            credit=entry.attribution,
            modified=bool(entry.modification_notice),
            basis="bundled_manifest",
        ),
    )


def load_starter_tracks(*, require_complete: bool = True) -> list[Track]:
    """Return only approved, hash-valid starter tracks; pending rows yield none."""

    catalog = load_starter_catalog(require_complete=require_complete, verify_all=True)
    return [starter_track(entry) for entry in catalog.entries]


def load_starter_rotation_tracks(*, seed: str | int | None = None, catalog_path: Path = CATALOG_PATH) -> list[Track]:
    """Drain exactly one verified, manifest-digest-pinned shuffle-bag cycle."""

    catalog = load_starter_catalog(catalog_path, require_complete=True, verify_all=False)
    bag = StarterShuffleBag(catalog, seed=seed)
    return [starter_track(bag.next_track()) for _ in catalog.entries]


def resolve_starter_track(track: Track, *, catalog_path: Path = CATALOG_PATH) -> StarterEntry:
    """Fail closed unless a runtime track exactly matches one approved row."""

    if track.source != "starter" or not track.provider_track_id:
        raise StarterCatalogError("runtime track is not a manifested starter track")
    catalog = load_starter_catalog(catalog_path, require_complete=True, verify_all=False)
    matches = [entry for entry in catalog.entries if entry.isrc == track.provider_track_id]
    if len(matches) != 1:
        raise StarterCatalogError(f"starter ISRC is not uniquely approved: {track.provider_track_id}")
    entry = matches[0]
    expected = starter_track(entry)
    immutable_match = (
        track.title == expected.title
        and track.artist == expected.artist
        and track.duration_ms == expected.duration_ms
        and track.spotify_id == expected.spotify_id
        and track.youtube_id == ""
        and track.direct_url == ""
        and track.local_path == expected.local_path
        and track.attribution == expected.attribution
    )
    if not immutable_match:
        raise StarterCatalogError(f"runtime facts do not match starter manifest: {track.provider_track_id}")
    verify_starter_entry(entry)
    return entry


def starter_source(track_count: int) -> PlaylistSource:
    """Describe the packaged source without reviving the legacy demo label."""

    return PlaylistSource(
        kind="starter",
        source_id="starter-v1",
        label="Bundled attributed starter",
        track_count=track_count,
        selected_at=time.time(),
        url="starter://catalog/v1",
    )

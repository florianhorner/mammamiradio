"""Fail-closed manifest for packaged audio that can enter speech lanes."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from mammamiradio.core.listener_truth import contains_unsafe_listener_claims
from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR
from mammamiradio.core.path_safety import safe_path_within

MANIFEST_FILENAME = "spoken_assets.json"
DISCOVERABLE_AUDIO_SUBDIRS = ("recovery", "banter", "first_listen", "ads")
PACKAGED_BANTER_PREDECESSOR_STARTER_ID_KEY = "_packaged_banter_predecessor_starter_id"

# A packaged advertisement is a finished spot, not a brand name read aloud. The
# station shipped a no-key fallback that spoke two sentences and called it an ad;
# these bounds are what made that unpackageable rather than merely discouraged.
# Both ends matter: below the floor there is no room for a premise, escalation
# and payoff, and above the ceiling a spot outstays a music break.
PACKAGED_AD_MIN_SECONDS = 25.0
PACKAGED_AD_MAX_SECONDS = 40.0
# The declared length is only a claim until the audio agrees with it. The
# measurement trims encoder priming and padding when the Info frame records
# them, as ffprobe does; a tenth of a second covers rounding in the declaration
# and an encoder that records neither, and nothing longer.
PACKAGED_AD_DURATION_TOLERANCE_SECONDS = 0.1
# 40 seconds at 320 kbps is about 1.6 MB; anything far beyond that is not a spot.
PACKAGED_AD_MAX_BYTES = 4 * 1024 * 1024
PACKAGED_AD_TITLE_MAX_CHARS = 80
PACKAGED_AD_CAST_MAX_MEMBERS = 6
PACKAGED_AD_CAST_MEMBER_MAX_CHARS = 40

# Modes the station speaks in, and the language each one must be recorded in.
_MODE_LANGUAGES = {"normal": "en", "super_italian": "it"}


@dataclass(frozen=True, slots=True)
class SpokenAssetEntry:
    """One content-addressed packaged-audio declaration."""

    relative_path: str
    sha256: str
    kind: str
    language: str
    transcript: str
    mode: str = ""
    required_previous_starter_id: str = ""
    special: bool = False
    duration_seconds: float = 0.0
    title: str = ""
    cast: tuple[str, ...] = ()


def validate_spoken_asset_manifest(*, assets_root: Path = DEMO_ASSETS_DIR) -> list[str]:
    """Return all schema, inventory, hash, and listener-truth errors."""

    root = Path(assets_root)
    data, errors = _read_manifest(root)
    if data is None:
        return errors
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list):
        errors.append("assets must be a list")
        return errors

    declared: set[str] = set()
    for index, raw in enumerate(raw_assets):
        prefix = f"assets[{index}]"
        entry, entry_errors = _parse_entry(raw, root=root, prefix=prefix)
        errors.extend(entry_errors)
        if entry is None:
            continue
        if entry.relative_path in declared:
            errors.append(f"{prefix}.path duplicates {entry.relative_path}")
            continue
        declared.add(entry.relative_path)
        asset_path = root / entry.relative_path
        if not asset_path.is_file():
            errors.append(f"{entry.relative_path} is missing")
            continue
        try:
            actual_sha256 = _sha256(asset_path)
        except OSError as exc:
            errors.append(f"{entry.relative_path} is unreadable: {exc}")
        else:
            if actual_sha256 != entry.sha256:
                errors.append(f"{entry.relative_path} sha256 does not match")
        errors.extend(_entry_policy_errors(entry))
        if Path(entry.relative_path).parts[0] == "ads":
            errors.extend(_ad_media_errors(asset_path, entry))

    discoverable = {
        path.relative_to(root).as_posix()
        for subdir in DISCOVERABLE_AUDIO_SUBDIRS
        for path in (root / subdir).glob("*.mp3")
        if path.is_file()
    }
    for relative_path in sorted(discoverable - declared):
        errors.append(f"{relative_path} is unlisted packaged audio")
    return errors


def approved_spoken_assets(subdir: str, *, assets_root: Path = DEMO_ASSETS_DIR) -> list[Path]:
    """Return hash-valid, truth-safe speech entries in one runtime subdirectory."""

    root = Path(assets_root)
    return [root / entry.relative_path for entry in approved_spoken_asset_entries(subdir, assets_root=root)]


def approved_spoken_asset_entries(
    subdir: str,
    *,
    assets_root: Path = DEMO_ASSETS_DIR,
) -> list[SpokenAssetEntry]:
    """Return validated declarations while preserving runtime selection metadata."""

    root = Path(assets_root)
    return [
        entry
        for entry in declared_spoken_asset_entries(subdir, assets_root=root)
        if _approved_manifest_entry(root / entry.relative_path, assets_root=root) == entry
    ]


def declared_spoken_asset_entries(
    subdir: str,
    *,
    assets_root: Path = DEMO_ASSETS_DIR,
) -> list[SpokenAssetEntry]:
    """Return policy-valid declarations without reading every packaged audio file.

    Runtime selectors cache these small manifest records, then hash only the
    selected file at admission. Whole-inventory hash and discovery checks stay
    in the release validator, away from first-byte and recovery paths.
    """

    if subdir not in DISCOVERABLE_AUDIO_SUBDIRS:
        return []
    root = Path(assets_root)
    data, _errors = _read_manifest(root)
    if data is None or data.get("schema_version") != 1:
        return []
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list):
        return []
    approved: list[SpokenAssetEntry] = []
    for index, raw in enumerate(raw_assets):
        entry, entry_errors = _parse_entry(raw, root=root, prefix=f"assets[{index}]")
        if entry is None or entry_errors or _entry_policy_errors(entry) or entry.kind != "speech":
            continue
        if Path(entry.relative_path).parent.as_posix() != subdir:
            continue
        approved.append(entry)
    return approved


def is_approved_spoken_asset(path: Path, *, assets_root: Path = DEMO_ASSETS_DIR) -> bool:
    """Revalidate one cached path so a changed asset fails closed immediately."""

    entry = _approved_manifest_entry(path, assets_root=assets_root)
    return entry is not None and entry.kind == "speech"


def approved_spoken_asset_entry(
    path: Path,
    *,
    assets_root: Path = DEMO_ASSETS_DIR,
) -> SpokenAssetEntry | None:
    """Return the hash-bound declaration for one approved speech asset."""

    entry = _approved_manifest_entry(path, assets_root=assets_root)
    return entry if entry is not None and entry.kind == "speech" else None


def _stays_inside_root(candidate: Path, root: Path) -> bool:
    """Whether ``candidate`` is still inside ``root`` once symlinks are followed.

    Thin wrapper over the shared containment helper so this module cannot drift
    from the cache and handoff paths that ask the same question. The symlink
    cycle handling that Python 3.13 made necessary lives there, in one place.
    """

    return safe_path_within(candidate, root) is not None


def is_approved_packaged_audio_asset(path: Path, *, assets_root: Path = DEMO_ASSETS_DIR) -> bool:
    """Return whether a speech or tone asset is declared, intact, and safe."""

    return _approved_manifest_entry(path, assets_root=assets_root) is not None


def _approved_manifest_entry(path: Path, *, assets_root: Path) -> SpokenAssetEntry | None:
    """Validate only the selected manifest entry and its content-addressed file."""

    candidate = Path(path)
    root = Path(assets_root)
    if not _stays_inside_root(candidate, root):
        return None
    try:
        relative = candidate.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    data, _errors = _read_manifest(root)
    if data is None or data.get("schema_version") != 1:
        return None
    raw_assets = data.get("assets") if data is not None else None
    if not isinstance(raw_assets, list):
        return None
    matching = [
        (index, raw)
        for index, raw in enumerate(raw_assets)
        if isinstance(raw, dict) and raw.get("path") == relative.as_posix()
    ]
    if len(matching) != 1:
        return None
    index, raw = matching[0]
    entry, entry_errors = _parse_entry(raw, root=root, prefix=f"assets[{index}]")
    if entry is None or entry_errors or _entry_policy_errors(entry):
        return None
    try:
        if candidate.is_file() and _sha256(candidate) == entry.sha256:
            return entry
    except OSError:
        return None
    return None


def _entry_policy_errors(entry: SpokenAssetEntry) -> list[str]:
    """Return listener-truth and lane-policy errors for one parsed entry."""

    errors: list[str] = []
    if entry.kind == "speech":
        if entry.language not in {"en", "it"}:
            errors.append(f"{entry.relative_path} speech language must be en or it")
        if not entry.transcript.strip():
            errors.append(f"{entry.relative_path} speech transcript is empty")
        elif contains_unsafe_listener_claims(entry.transcript):
            errors.append(f"{entry.relative_path} transcript contains listener arrival/return copy")
    elif entry.kind == "tone":
        if entry.language != "none" or entry.transcript:
            errors.append(f"{entry.relative_path} tone must use language=none and an empty transcript")
    else:
        errors.append(f"{entry.relative_path} kind must be speech or tone")

    subdir = Path(entry.relative_path).parent.as_posix()
    if subdir == "banter":
        errors.extend(_mode_language_errors(entry, "banter"))
        starter_id = entry.required_previous_starter_id
        if starter_id and (len(starter_id) > 80 or any(not (char.isalnum() or char in "._-") for char in starter_id)):
            errors.append(f"{entry.relative_path} required starter id is invalid")
        if entry.special and (entry.mode != "normal" or starter_id):
            errors.append(f"{entry.relative_path} special banter must be evergreen Normal Mode copy")
    elif Path(entry.relative_path).parts[0] == "ads":
        errors.extend(_ad_entry_policy_errors(entry))
    elif entry.mode or entry.required_previous_starter_id or entry.special:
        errors.append(f"{entry.relative_path} non-banter asset has banter metadata")
    return errors


def _ad_entry_policy_errors(entry: SpokenAssetEntry) -> list[str]:
    """Return the policy errors that keep a packaged spot a finished advertisement.

    A packaged ad is mode-bound like banter, but it carries no adjacency: an ad
    never depends on the song before it, and there is no rare-special tier. The
    duration floor is the part that matters most. The station's no-key fallback
    spoke the brand name and a tagline, rendered successfully, and aired as an
    advertisement; nothing in the packaging contract could object, because the
    contract had no opinion about how long an advertisement is.
    """

    errors = _mode_language_errors(entry, "ad")
    if Path(entry.relative_path).parent.as_posix() != "ads":
        # Discovery and runtime selection both read ads/ flat, so a nested spot
        # would be invisible to one and unchecked by the other.
        errors.append(f"{entry.relative_path} packaged ad must sit directly in ads/")
    if entry.required_previous_starter_id or entry.special:
        errors.append(f"{entry.relative_path} ad must not carry banter adjacency metadata")
    if entry.duration_seconds <= 0.0:
        errors.append(f"{entry.relative_path} ad must declare duration_seconds")
    elif not PACKAGED_AD_MIN_SECONDS <= entry.duration_seconds <= PACKAGED_AD_MAX_SECONDS:
        errors.append(
            f"{entry.relative_path} ad is {entry.duration_seconds:g}s; "
            f"a packaged ad runs {PACKAGED_AD_MIN_SECONDS:g}-{PACKAGED_AD_MAX_SECONDS:g}s"
        )
    if not entry.title.strip():
        errors.append(f"{entry.relative_path} ad must declare a title")
    elif len(entry.title) > PACKAGED_AD_TITLE_MAX_CHARS:
        errors.append(f"{entry.relative_path} ad title exceeds {PACKAGED_AD_TITLE_MAX_CHARS} characters")
    if not entry.cast:
        errors.append(f"{entry.relative_path} ad must declare a cast")
    elif len(entry.cast) > PACKAGED_AD_CAST_MAX_MEMBERS:
        errors.append(f"{entry.relative_path} ad cast exceeds {PACKAGED_AD_CAST_MAX_MEMBERS} members")
    if any(not member.strip() or len(member) > PACKAGED_AD_CAST_MEMBER_MAX_CHARS for member in entry.cast):
        errors.append(f"{entry.relative_path} ad cast members must be 1-{PACKAGED_AD_CAST_MEMBER_MAX_CHARS} characters")
    return errors


def _mode_language_errors(entry: SpokenAssetEntry, noun: str) -> list[str]:
    """Return errors when an entry's mode is unknown or its language does not match."""

    errors: list[str] = []
    if entry.mode not in _MODE_LANGUAGES:
        errors.append(f"{entry.relative_path} {noun} mode must be normal or super_italian")
    expected_language = _MODE_LANGUAGES.get(entry.mode)
    if expected_language is not None and entry.language != expected_language:
        errors.append(f"{entry.relative_path} {noun} language does not match its mode")
    return errors


def _read_manifest(root: Path) -> tuple[dict[str, object] | None, list[str]]:
    manifest_path = root / MANIFEST_FILENAME
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"{MANIFEST_FILENAME} is missing"]
    except (OSError, ValueError) as exc:
        # ValueError covers JSONDecodeError and UnicodeDecodeError, and also the
        # plain ValueError json.loads raises for an integer longer than 4300
        # digits. The rescue ladder reads this manifest and must never raise.
        return None, [f"{MANIFEST_FILENAME} is unreadable: {exc}"]
    if not isinstance(raw, dict):
        return None, [f"{MANIFEST_FILENAME} root must be an object"]
    return raw, []


def _parse_entry(raw: object, *, root: Path, prefix: str) -> tuple[SpokenAssetEntry | None, list[str]]:
    if not isinstance(raw, dict):
        return None, [f"{prefix} must be an object"]
    values = {key: raw.get(key) for key in ("path", "sha256", "kind", "language", "transcript")}
    if not all(isinstance(value, str) for value in values.values()):
        return None, [f"{prefix} fields must all be strings"]
    mode = raw.get("mode", "")
    required_previous_starter_id = raw.get("required_previous_starter_id", "")
    special = raw.get("special", False)
    if not isinstance(mode, str) or not isinstance(required_previous_starter_id, str) or not isinstance(special, bool):
        return None, [f"{prefix} banter metadata has invalid types"]
    relative_path = str(values["path"])
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".mp3":
        return None, [f"{prefix}.path must be a safe relative mp3 path"]
    if relative.parts[:1] not in {(name,) for name in DISCOVERABLE_AUDIO_SUBDIRS}:
        return None, [f"{prefix}.path is outside the packaged speech inventory"]
    digest = str(values["sha256"]).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None, [f"{prefix}.sha256 must be 64 lowercase hex characters"]
    if not _stays_inside_root(root / relative, root):
        return None, [f"{prefix}.path escapes the asset root"]
    duration_seconds, duration_error = _parse_duration(raw.get("duration_seconds", 0))
    if duration_error:
        if relative.parts[0] == "ads":
            return None, [f"{prefix}.duration_seconds {duration_error}"]
        # Only ads read a duration. Every other category ignored the field before
        # it existed, and a typo in it must not drop a recovery clip from the
        # rescue ladder.
        duration_seconds = 0.0
    title = raw.get("title", "")
    raw_cast = raw.get("cast", [])
    if relative.parts[0] == "ads":
        if not isinstance(title, str):
            return None, [f"{prefix}.title must be a string"]
        if not isinstance(raw_cast, list) or not all(isinstance(member, str) for member in raw_cast):
            return None, [f"{prefix}.cast must be a list of strings"]
        cast = tuple(raw_cast)
    else:
        title = ""
        cast = ()
    return (
        SpokenAssetEntry(
            relative_path=relative.as_posix(),
            sha256=digest,
            kind=str(values["kind"]),
            language=str(values["language"]),
            transcript=str(values["transcript"]),
            mode=mode,
            required_previous_starter_id=required_previous_starter_id,
            special=special,
            duration_seconds=duration_seconds,
            title=title,
            cast=cast,
        ),
        [],
    )


def _parse_duration(raw_duration: object) -> tuple[float, str]:
    """Return ``(seconds, "")`` or ``(0.0, reason)`` without ever raising."""

    # ``bool`` is an ``int`` subclass, so ``True`` would otherwise read as 1.0 and
    # declare a one-second advertisement.
    if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)):
        return 0.0, "must be a number"
    # JSON integers are unbounded, and ``float()`` raises on one too large to
    # represent. This parser sits under the dead-air rescue ladder, which must
    # never raise, so conversion failure is a reason, not an exception.
    try:
        duration_seconds = float(raw_duration)
    except (OverflowError, ValueError):
        return 0.0, "must be a non-negative finite number"
    if not math.isfinite(duration_seconds) or duration_seconds < 0.0:
        return 0.0, "must be a non-negative finite number"
    return duration_seconds, ""


def _ad_media_errors(path: Path, entry: SpokenAssetEntry) -> list[str]:
    """Return errors unless the MP3 itself runs as long as the manifest says.

    Only the release validator calls this. Runtime admission stays hash-only:
    the hash binds the file to the recording measured here.
    """

    try:
        actual = _mp3_duration_seconds(path)
    except OSError as exc:
        return [f"{entry.relative_path} is unreadable: {exc}"]
    except ValueError as exc:
        return [f"{entry.relative_path} is not decodable MP3 audio: {exc}"]
    errors: list[str] = []
    slack = PACKAGED_AD_DURATION_TOLERANCE_SECONDS
    if not PACKAGED_AD_MIN_SECONDS - slack <= actual <= PACKAGED_AD_MAX_SECONDS + slack:
        errors.append(
            f"{entry.relative_path} audio runs {actual:.3f}s; "
            f"a packaged ad runs {PACKAGED_AD_MIN_SECONDS:g}-{PACKAGED_AD_MAX_SECONDS:g}s"
        )
    if entry.duration_seconds > 0.0 and abs(actual - entry.duration_seconds) > PACKAGED_AD_DURATION_TOLERANCE_SECONDS:
        errors.append(f"{entry.relative_path} declares {entry.duration_seconds:g}s but the audio runs {actual:.3f}s")
    return errors


# MPEG audio Layer III tables, indexed by the header's version bits.
_MPEG1 = 3
_MP3_BITRATES_KBPS = {
    _MPEG1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),  # MPEG-2
    0: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),  # MPEG-2.5
}
_MP3_SAMPLE_RATES_HZ = {_MPEG1: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_duration_seconds(path: Path) -> float:
    """Measure an MP3 by walking every Layer III frame, without decoding it.

    Stdlib-only so the release gate can run it under macOS's Python 3.9. The
    walk is strict: after an optional ID3v2 tag, every byte must belong to a
    frame, apart from one trailing 128-byte ID3v1 tag, and every frame must
    share the first frame's MPEG version and sample rate. A truncated or padded
    file is refused rather than estimated. A Xing/Info header frame carries no
    audio and is not counted.
    """

    with Path(path).open("rb") as handle:
        data = handle.read(PACKAGED_AD_MAX_BYTES + 1)
    if len(data) > PACKAGED_AD_MAX_BYTES:
        raise ValueError(f"file exceeds {PACKAGED_AD_MAX_BYTES} bytes")
    offset = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size_bytes = data[6:10]
        if any(byte & 0x80 for byte in size_bytes):
            raise ValueError("ID3v2 size is not syncsafe")
        size = 0
        for byte in size_bytes:
            size = (size << 7) | byte
        offset = 10 + size + (10 if data[5] & 0x10 else 0)
    try:
        return _walk_mp3_frames(data, offset, len(data))
    except ValueError:
        # "TAG" 128 bytes from the end is only an ID3v1 tag if the frames stop
        # exactly there; audio bytes that happen to spell it must not be cut.
        if len(data) - offset < 128 or data[-128:-125] != b"TAG":
            raise
        return _walk_mp3_frames(data, offset, len(data) - 128)


def _walk_mp3_frames(data: bytes, offset: int, end: int) -> float:
    seconds = 0.0
    frames = 0
    trim_samples = 0
    trim_rate = 0
    stream: tuple[int, int] | None = None
    while offset < end:
        if end - offset < 4:
            raise ValueError(f"trailing bytes at offset {offset}")
        header = int.from_bytes(data[offset : offset + 4], "big")
        version = (header >> 19) & 0b11
        bitrate_index = (header >> 12) & 0b1111
        rate_index = (header >> 10) & 0b11
        if (
            (header >> 21) != 0x7FF
            or version == 1
            or (header >> 17) & 0b11 != 0b01
            or bitrate_index in (0, 15)
            or rate_index == 3
        ):
            raise ValueError(f"no Layer III frame at offset {offset}")
        bitrate = _MP3_BITRATES_KBPS[version][bitrate_index] * 1000
        sample_rate = _MP3_SAMPLE_RATES_HZ[version][rate_index]
        if stream is None:
            stream = (version, sample_rate)
        elif stream != (version, sample_rate):
            raise ValueError(f"frame at offset {offset} changes MPEG version or sample rate")
        samples = 1152 if version == _MPEG1 else 576
        length = samples // 8 * bitrate // sample_rate + ((header >> 9) & 1)
        if offset + length > end:
            raise ValueError(f"truncated frame at offset {offset}")
        gapless = _xing_gapless_trim(data[offset : offset + length], version, header) if frames == 0 else None
        if gapless is None:
            seconds += samples / sample_rate
        else:
            trim_samples, trim_rate = gapless, sample_rate
        frames += 1
        offset += length
    if trim_rate:
        # Encoder priming and end padding are silence the decoder drops, which
        # is how ffprobe reports the length too.
        seconds -= trim_samples / trim_rate
    if seconds <= 0.0:
        raise ValueError("no audio frames")
    return seconds


def _xing_gapless_trim(frame: bytes, version: int, header: int) -> int | None:
    """Return None for an audio frame, else the samples a Xing/Info frame says to trim.

    A Xing/Info frame carries no audio. When it also carries a LAME extension,
    that records the encoder delay and end padding in samples.
    """

    mono = (header >> 6) & 0b11 == 0b11
    # The tag follows the 4-byte header and the side information.
    side_info = (17 if mono else 32) if version == _MPEG1 else (9 if mono else 17)
    tag = 4 + side_info
    if frame[tag : tag + 4] not in (b"Xing", b"Info"):
        return None
    flags = int.from_bytes(frame[tag + 4 : tag + 8], "big")
    lame = tag + 8 + 4 * bool(flags & 1) + 4 * bool(flags & 2) + 100 * bool(flags & 4) + 4 * bool(flags & 8)
    gapless = frame[lame + 21 : lame + 24]
    if frame[lame : lame + 4] not in (b"LAME", b"Lavc", b"Lavf") or len(gapless) != 3:
        return 0
    packed = int.from_bytes(gapless, "big")
    return (packed >> 12) + (packed & 0xFFF)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

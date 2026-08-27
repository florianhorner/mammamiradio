"""Squash-safe, content-addressed pre-ship review receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterator, Set
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

from .errors import EvidenceError, GitError, LandingError
from .gitops import GitRepository, TreeEntry

EXPECTED_REPOSITORY = "florianhorner/mammamiradio"
SCHEMA_VERSION = "2.0.0"
RECEIPT_KIND = "mammamiradio.preship-review"
LEGACY_CONTENT_PROFILE = "git-ls-tree-rz-full-tree-minus-v2-receipts-v1"
CONTENT_PROFILE = "git-ls-tree-rz-full-tree-minus-v2-and-ha-green-receipts-v2"
SUPPORTED_CONTENT_PROFILES = frozenset({LEGACY_CONTENT_PROFILE, CONTENT_PROFILE})
RECEIPT_ROOT = "proof/preship-reviews/v2"
DEFAULT_LANDED_REF = "origin/main"
RECEIPT_NAMESPACE_BYTES = RECEIPT_ROOT.encode("ascii")
RECEIPT_ROOT_BYTES = (RECEIPT_ROOT + "/").encode("ascii")
ALLOWED_SKILLS = frozenset({"review", "adversarial-review"})
MAX_RECEIPT_BYTES = 16 * 1024
RECEIPT_READ_BATCH_SIZE = 64
MAX_RECEIPT_BATCH_BYTES = MAX_RECEIPT_BYTES * RECEIPT_READ_BATCH_SIZE
MAX_NEW_RECEIPTS = 32
MAX_RECEIPT_PATH_BYTES = 16 * 1024
MAX_LEDGER_LINE_BYTES = 256 * 1024
MAX_TREE_ENTRIES = 250_000
MAX_TREE_BYTES = 256 * 1024 * 1024
MAX_TREE_RECORD_BYTES = 64 * 1024
HA_RECEIPT_ROOT = "proof/media/ha-green-release-evidence"
HA_RECEIPT_ROOT_BYTES = (HA_RECEIPT_ROOT + "/").encode("ascii")
MAX_HA_RECEIPT_BYTES = 64 * 1024
MAX_HA_RECEIPTS = 1_000

_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_PATH_RE = re.compile(rb"^proof/preship-reviews/v2/([0-9a-f]{64})/([0-9a-f]{64})\.json$")
_HA_RECEIPT_PATH_RE = re.compile(
    rb"^proof/media/ha-green-release-evidence/run-"
    rb"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json$"
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "content_profile",
        "kind",
        "repository",
        "review",
        "reviewed_commit",
        "reviewed_content_sha256",
        "schema_version",
        "source_record_sha256",
    }
)
_REVIEW_KEYS = frozenset({"skill", "status", "timestamp"})
_FINDING_KEYS = frozenset({"fingerprint", "severity", "action"})
_FINDING_KEYS_WITH_REASON = _FINDING_KEYS | {"reason"}
_FINDING_SEVERITIES = frozenset({"CRITICAL", "INFORMATIONAL"})
_FINDING_ACTIONS = frozenset({"auto-fixed", "fixed", "skipped"})
_FINDING_FINGERPRINT_RE = re.compile(
    r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!\.\.?:)[^:\n]+:[1-9][0-9]*:[a-z0-9]+(?:-[a-z0-9]+)*$"
)


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _decode_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError(f"{label} is not valid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, RecursionError) as exc:
        raise EvidenceError(f"{label} is not valid single-document JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must contain one JSON object")
    return value


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise EvidenceError(f"receipt cannot be serialized canonically: {exc}") from exc
    return encoded + b"\n"


def _parse_aware_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise EvidenceError("review timestamp must be a non-empty string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, OverflowError) as exc:
        raise EvidenceError(f"review timestamp {value!r} is not ISO-8601") from exc
    try:
        offset = parsed.utcoffset()
    except OverflowError as exc:
        raise EvidenceError(f"review timestamp {value!r} is outside the supported range") from exc
    if parsed.tzinfo is None or offset is None:
        raise EvidenceError(f"review timestamp {value!r} has no timezone")
    try:
        return parsed.astimezone(UTC)
    except OverflowError as exc:
        raise EvidenceError(f"review timestamp {value!r} is outside the supported range") from exc


def _display_path(path: bytes) -> str:
    return repr(os.fsdecode(path))


def repository_identity(repo: GitRepository) -> str:
    """Return the verified GitHub owner/repository identity for ``origin``."""

    url = repo.origin_url().strip()
    identity: str | None = None

    scp_match = re.fullmatch(r"(?:[^@/]+@)?github\.com:([^?#]+)", url, flags=re.IGNORECASE)
    if scp_match:
        remote_path = scp_match.group(1)
    else:
        parsed = urlparse(url)
        if parsed.scheme not in {"git", "https", "ssh"} or (parsed.hostname or "").lower() != "github.com":
            raise EvidenceError("origin must be a github.com repository URL")
        remote_path = parsed.path.lstrip("/")

    remote_path = remote_path.rstrip("/")
    if remote_path.lower().endswith(".git"):
        remote_path = remote_path[:-4]
    parts = [part for part in remote_path.split("/") if part]
    if len(parts) == 2:
        identity = f"{parts[0]}/{parts[1]}".lower()
    if identity != EXPECTED_REPOSITORY:
        rendered = identity or "unrecognized"
        raise EvidenceError(f"origin identifies {rendered!r}; v2 evidence is scoped to {EXPECTED_REPOSITORY!r}")
    return identity


@dataclass(frozen=True)
class Receipt:
    path: bytes
    reviewed_content_sha256: str
    receipt_sha256: str
    reviewed_commit: str
    content_profile: str


@dataclass(frozen=True)
class TreeSnapshot:
    commit: str
    content_sha256: str
    # Callers choose the minimal receipt subset they need. Ordinary tree records
    # and validated historical receipts are never retained by production paths.
    receipts: dict[bytes, Receipt]
    has_ha_green_receipts: bool


@dataclass(frozen=True)
class VerificationResult:
    mode: str
    target: str
    content_sha256: str
    matching_receipts: tuple[str, ...]


def _validate_receipt(
    *,
    repo: GitRepository,
    repository: str,
    entry: TreeEntry,
    raw: bytes,
) -> Receipt:
    path_match = _RECEIPT_PATH_RE.fullmatch(entry.path)
    if path_match is None:
        raise EvidenceError(f"unknown entry in reserved v2 namespace: {_display_path(entry.path)}")
    if entry.mode != b"100644" or entry.kind != b"blob":
        mode = entry.mode.decode("ascii", errors="replace")
        kind = entry.kind.decode("ascii", errors="replace")
        raise EvidenceError(
            f"v2 receipt {_display_path(entry.path)} must be a non-executable regular blob, got {mode} {kind}"
        )

    payload = _decode_json_object(raw, label=f"v2 receipt {_display_path(entry.path)}")
    if canonical_json_bytes(payload) != raw:
        raise EvidenceError(
            f"v2 receipt {_display_path(entry.path)} is not canonical one-line JSON with one trailing LF"
        )
    if frozenset(payload) != _TOP_LEVEL_KEYS:
        raise EvidenceError(f"v2 receipt {_display_path(entry.path)} has missing or unknown top-level keys")

    review = payload.get("review")
    if not isinstance(review, dict) or frozenset(review) != _REVIEW_KEYS:
        raise EvidenceError(f"v2 receipt {_display_path(entry.path)} has an invalid review object")

    expected_values = {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "repository": repository,
    }
    for key, expected in expected_values.items():
        if payload.get(key) != expected:
            raise EvidenceError(
                f"v2 receipt {_display_path(entry.path)} has {key}={payload.get(key)!r}; expected {expected!r}"
            )
    content_profile = payload.get("content_profile")
    if not isinstance(content_profile, str) or content_profile not in SUPPORTED_CONTENT_PROFILES:
        raise EvidenceError(
            f"v2 receipt {_display_path(entry.path)} has unsupported content_profile={content_profile!r}"
        )

    skill = review.get("skill")
    if skill not in ALLOWED_SKILLS:
        raise EvidenceError(f"v2 receipt {_display_path(entry.path)} has unsupported review skill {skill!r}")
    if review.get("status") != "clean":
        raise EvidenceError(f"v2 receipt {_display_path(entry.path)} does not record status='clean'")
    _parse_aware_timestamp(review.get("timestamp"))

    content_digest = payload.get("reviewed_content_sha256")
    source_digest = payload.get("source_record_sha256")
    reviewed_commit = payload.get("reviewed_commit")
    if not isinstance(content_digest, str) or _HEX_64_RE.fullmatch(content_digest) is None:
        raise EvidenceError(f"v2 receipt {_display_path(entry.path)} has an invalid content digest")
    if not isinstance(source_digest, str) or _HEX_64_RE.fullmatch(source_digest) is None:
        raise EvidenceError(f"v2 receipt {_display_path(entry.path)} has an invalid source-record digest")
    if (
        not isinstance(reviewed_commit, str)
        or len(reviewed_commit) != repo.oid_length
        or re.fullmatch(r"[0-9a-f]+", reviewed_commit) is None
    ):
        raise EvidenceError(f"v2 receipt {_display_path(entry.path)} has an invalid full reviewed commit ID")

    path_content_digest = path_match.group(1).decode("ascii")
    path_receipt_digest = path_match.group(2).decode("ascii")
    if path_content_digest != content_digest:
        raise EvidenceError(f"v2 receipt {_display_path(entry.path)} disagrees with its content directory")
    actual_receipt_digest = hashlib.sha256(raw).hexdigest()
    if path_receipt_digest != actual_receipt_digest:
        raise EvidenceError(f"v2 receipt {_display_path(entry.path)} disagrees with its filename digest")

    return Receipt(
        path=entry.path,
        reviewed_content_sha256=content_digest,
        receipt_sha256=actual_receipt_digest,
        reviewed_commit=reviewed_commit,
        content_profile=content_profile,
    )


@lru_cache(maxsize=1)
def _ha_release_validator() -> Any:
    name = "_mammamiradio_ha_green_release_validator_for_v2"
    path = Path(__file__).resolve().parents[1] / "validate-ha-green-release-evidence.py"
    module = ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def _validate_ha_release_receipt(*, entry: TreeEntry, raw: bytes) -> None:
    try:
        _ha_release_validator()._validate_receipt(Path(os.fsdecode(entry.path)), raw=raw)
    except ValueError as exc:
        raise EvidenceError(f"HA Green receipt {_display_path(entry.path)} is invalid: {exc}") from exc


def snapshot_tree(
    repo: GitRepository,
    commit: str,
    *,
    repository: str | None = None,
    receipt_paths: Set[bytes] = frozenset(),
    retain_all_receipts: bool = False,
    retain_matching_receipts: bool = False,
) -> TreeSnapshot:
    """Validate a tree, hash ordinary entries, and retain only requested receipts."""

    if retain_all_receipts and retain_matching_receipts:
        raise EvidenceError("receipt retention modes are mutually exclusive")
    resolved = repo.resolve_commit(commit)
    expected_repository = repository or repository_identity(repo)
    retained: dict[bytes, Receipt] = {}

    def validate_batch(batch: list[TreeEntry], *, keep_all: bool, ha_green: bool = False) -> None:
        if not batch:
            return
        max_bytes = MAX_HA_RECEIPT_BYTES if ha_green else MAX_RECEIPT_BYTES
        max_total_bytes = MAX_HA_RECEIPT_BYTES * RECEIPT_READ_BATCH_SIZE if ha_green else MAX_RECEIPT_BATCH_BYTES
        blobs = repo.read_blobs(
            (entry.oid for entry in batch),
            max_bytes=max_bytes,
            max_total_bytes=max_total_bytes,
        )
        for entry in batch:
            if ha_green:
                _validate_ha_release_receipt(entry=entry, raw=blobs[entry.oid])
                continue
            receipt = _validate_receipt(
                repo=repo,
                repository=expected_repository,
                entry=entry,
                raw=blobs[entry.oid],
            )
            if keep_all or entry.path in receipt_paths:
                retained[entry.path] = receipt

    digest = hashlib.sha256()
    previous_path: bytes | None = None
    ordinary_entries = 0
    ordinary_bytes = 0
    receipt_batch: list[TreeEntry] = []
    ha_receipt_batch: list[TreeEntry] = []
    ha_receipt_count = 0
    for entry in repo.tree_entries(resolved, max_record_bytes=MAX_TREE_RECORD_BYTES):
        if entry.path == previous_path:
            raise GitError(f"git ls-tree returned duplicate path {_display_path(entry.path)}")
        previous_path = entry.path
        if entry.path == RECEIPT_NAMESPACE_BYTES or entry.path.startswith(RECEIPT_ROOT_BYTES):
            if _RECEIPT_PATH_RE.fullmatch(entry.path) is None:
                raise EvidenceError(f"unknown entry in reserved v2 namespace: {_display_path(entry.path)}")
            if entry.mode != b"100644" or entry.kind != b"blob":
                mode = entry.mode.decode("ascii", errors="replace")
                kind = entry.kind.decode("ascii", errors="replace")
                raise EvidenceError(
                    f"v2 receipt {_display_path(entry.path)} must be a non-executable regular blob, got {mode} {kind}"
                )
            receipt_batch.append(entry)
            if len(receipt_batch) == RECEIPT_READ_BATCH_SIZE:
                validate_batch(receipt_batch, keep_all=retain_all_receipts)
                receipt_batch = []
            continue
        if _HA_RECEIPT_PATH_RE.fullmatch(entry.path) is not None:
            if entry.mode != b"100644" or entry.kind != b"blob":
                mode = entry.mode.decode("ascii", errors="replace")
                kind = entry.kind.decode("ascii", errors="replace")
                raise EvidenceError(
                    f"HA Green receipt {_display_path(entry.path)} must be a non-executable regular blob, "
                    f"got {mode} {kind}"
                )
            ha_receipt_count += 1
            if ha_receipt_count > MAX_HA_RECEIPTS:
                raise EvidenceError(f"tree contains more than {MAX_HA_RECEIPTS} HA Green receipts")
            ha_receipt_batch.append(entry)
            if len(ha_receipt_batch) == RECEIPT_READ_BATCH_SIZE:
                validate_batch(ha_receipt_batch, keep_all=False, ha_green=True)
                ha_receipt_batch = []
            continue

        ordinary_entries += 1
        if ordinary_entries > MAX_TREE_ENTRIES:
            raise GitError(f"git ls-tree returned more than {MAX_TREE_ENTRIES} ordinary entries")
        ordinary_bytes += len(entry.raw_with_nul)
        if ordinary_bytes > MAX_TREE_BYTES:
            raise GitError(f"git ls-tree returned more than {MAX_TREE_BYTES} ordinary bytes")
        digest.update(entry.raw_with_nul)
    validate_batch(receipt_batch, keep_all=retain_all_receipts)
    validate_batch(ha_receipt_batch, keep_all=False, ha_green=True)

    content_digest = digest.hexdigest()
    if retain_matching_receipts:
        matching_prefix = RECEIPT_ROOT_BYTES + content_digest.encode("ascii") + b"/"
        matching_batch: list[TreeEntry] = []
        for entry in repo.tree_entries(resolved, max_record_bytes=MAX_TREE_RECORD_BYTES):
            if not entry.path.startswith(matching_prefix):
                continue
            matching_batch.append(entry)
            if len(matching_batch) == RECEIPT_READ_BATCH_SIZE:
                validate_batch(matching_batch, keep_all=True)
                matching_batch = []
        validate_batch(matching_batch, keep_all=True)

    return TreeSnapshot(
        commit=resolved,
        content_sha256=content_digest,
        receipts=retained,
        has_ha_green_receipts=bool(ha_receipt_count),
    )


@dataclass(frozen=True)
class LedgerRecord:
    raw_sha256: str
    commit_ref: str
    timestamp_text: str
    timestamp: datetime | None
    skill: str
    status: object
    schema_error: str | None


def _ledger_record(raw_line: bytes) -> LedgerRecord | None:
    raw_sha256 = hashlib.sha256(raw_line).hexdigest()
    strict_error: str | None = None
    try:
        entry = _decode_json_object(raw_line, label="review ledger record")
    except EvidenceError as exc:
        strict_error = str(exc)
        try:
            decoded = raw_line.decode("utf-8")
            permissive = json.loads(decoded)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None
        if not isinstance(permissive, dict):
            return None
        entry = permissive

    skill = entry.get("skill")
    commit_ref = entry.get("commit")
    timestamp_text = entry.get("timestamp")
    if skill not in ALLOWED_SKILLS:
        return None
    if (
        not isinstance(commit_ref, str)
        or not 7 <= len(commit_ref) <= 64
        or re.fullmatch(r"[0-9a-fA-F]+", commit_ref) is None
    ):
        return None
    if not isinstance(timestamp_text, str):
        timestamp_text = ""

    timestamp: datetime | None
    try:
        timestamp = _parse_aware_timestamp(timestamp_text)
    except EvidenceError as exc:
        timestamp = None
        strict_error = strict_error or str(exc)

    findings = entry.get("findings")
    if not isinstance(findings, list):
        strict_error = strict_error or "review ledger record has non-list or missing findings"
    else:
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict) or frozenset(finding) not in {
                _FINDING_KEYS,
                _FINDING_KEYS_WITH_REASON,
            }:
                strict_error = strict_error or f"review ledger finding {index} has an invalid object shape"
                continue
            fingerprint = finding.get("fingerprint")
            severity = finding.get("severity")
            action = finding.get("action")
            reason = finding.get("reason")
            if not isinstance(fingerprint, str) or _FINDING_FINGERPRINT_RE.fullmatch(fingerprint) is None:
                strict_error = strict_error or f"review ledger finding {index} has an invalid fingerprint"
            if severity not in _FINDING_SEVERITIES:
                strict_error = strict_error or f"review ledger finding {index} has an invalid severity"
            if action not in _FINDING_ACTIONS:
                strict_error = strict_error or f"review ledger finding {index} has an invalid action"
            if "reason" in finding and (not isinstance(reason, str) or not reason.strip()):
                strict_error = strict_error or f"review ledger finding {index} has an invalid reason"
            if severity == "CRITICAL" and action == "skipped" and not (isinstance(reason, str) and reason.strip()):
                strict_error = strict_error or (
                    f"review ledger finding {index} skips a CRITICAL finding without a reason"
                )
            if entry.get("status") == "clean" and action == "skipped":
                strict_error = strict_error or (f"clean review ledger outcome leaves finding {index} skipped")
    status = entry.get("status")
    if not isinstance(status, str):
        strict_error = strict_error or "review ledger record has non-string or missing status"

    return LedgerRecord(
        raw_sha256=raw_sha256,
        commit_ref=commit_ref,
        timestamp_text=timestamp_text,
        timestamp=timestamp,
        skill=skill,
        status=status,
        schema_error=strict_error,
    )


def _ledger_files(project_dir: Path) -> Iterator[Path]:
    def fail_walk(error: OSError) -> None:
        raise EvidenceError(f"cannot scan review ledger directory {project_dir}: {error}")

    for dirpath, dirnames, filenames in os.walk(project_dir, followlinks=False, onerror=fail_walk):
        dirnames[:] = sorted(name for name in dirnames if not (Path(dirpath) / name).is_symlink())
        for filename in sorted(filenames):
            if not filename.endswith("-reviews.jsonl"):
                continue
            path = Path(dirpath) / filename
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def _iter_ledger_records(project_dir: Path) -> Iterator[LedgerRecord]:
    for path in _ledger_files(project_dir):
        try:
            with path.open("rb") as handle:
                while raw_line := handle.readline(MAX_LEDGER_LINE_BYTES + 1):
                    if len(raw_line) > MAX_LEDGER_LINE_BYTES:
                        raise EvidenceError(f"review ledger line in {path} exceeds {MAX_LEDGER_LINE_BYTES} bytes")
                    if not raw_line.strip() or raw_line.lstrip().startswith(b"---"):
                        continue
                    record = _ledger_record(raw_line)
                    if record is not None:
                        yield record
        except OSError as exc:
            raise EvidenceError(f"cannot read review ledger file {path}: {exc}") from exc


def _read_ledger(project_dir: Path) -> list[LedgerRecord]:
    return list(_iter_ledger_records(project_dir))


def select_review_record(
    repo: GitRepository,
    *,
    target_content_sha256: str,
    repository: str,
    ledger_root: Path | None = None,
) -> tuple[LedgerRecord, str]:
    root = ledger_root or Path(os.environ.get("GSTACK_HOME", os.path.expanduser("~/.gstack")))
    project_dir = root / "projects" / EXPECTED_REPOSITORY.replace("/", "-")
    if project_dir.is_symlink() or not project_dir.is_dir():
        raise EvidenceError(
            f"no exact review ledger directory at {project_dir}; run the review skill in {EXPECTED_REPOSITORY}"
        )

    @lru_cache(maxsize=1024)
    def resolve_record(commit_ref: str) -> str | None:
        try:
            return repo.resolve_abbreviated_commit(commit_ref)
        except GitError:
            return None

    @lru_cache(maxsize=256)
    def reviewed_digest(resolved: str) -> str | None:
        try:
            return snapshot_tree(repo, resolved, repository=repository).content_sha256
        except LandingError:
            return None

    saw_record = False
    latest_timestamp: datetime | None = None
    review_seen = False
    adversarial_seen = False
    review_errors: set[str] = set()
    adversarial_errors: set[str] = set()
    review_statuses: set[str] = set()
    adversarial_statuses: set[str] = set()
    review_selected: tuple[LedgerRecord, str] | None = None
    adversarial_selected: tuple[LedgerRecord, str] | None = None

    for record in _iter_ledger_records(project_dir):
        saw_record = True
        resolved = resolve_record(record.commit_ref)
        if resolved is None or reviewed_digest(resolved) != target_content_sha256:
            continue
        if record.timestamp is None:
            raise EvidenceError(
                f"matching {record.skill} ledger record has an invalid timestamp: {record.schema_error}"
            )
        if latest_timestamp is None or record.timestamp > latest_timestamp:
            latest_timestamp = record.timestamp
            review_seen = False
            adversarial_seen = False
            review_errors.clear()
            adversarial_errors.clear()
            review_statuses.clear()
            adversarial_statuses.clear()
            review_selected = None
            adversarial_selected = None
        if record.timestamp != latest_timestamp:
            continue

        is_review = record.skill == "review"
        if is_review:
            review_seen = True
            errors = review_errors
            statuses = review_statuses
            selected = review_selected
        else:
            adversarial_seen = True
            errors = adversarial_errors
            statuses = adversarial_statuses
            selected = adversarial_selected
        if record.schema_error:
            errors.add(record.schema_error)
        else:
            statuses.add(str(record.status))
        candidate = (record, resolved)
        if selected is None or (record.raw_sha256, resolved) < (selected[0].raw_sha256, selected[1]):
            selected = candidate
        if is_review:
            review_selected = selected
        else:
            adversarial_selected = selected

    if not saw_record:
        raise EvidenceError(f"no review records found under exact project directory {project_dir}")

    if review_seen:
        errors = review_errors
        statuses = review_statuses
        selected = review_selected
    elif adversarial_seen:
        errors = adversarial_errors
        statuses = adversarial_statuses
        selected = adversarial_selected
    else:
        errors = set()
        statuses = set()
        selected = None

    if selected is None:
        raise EvidenceError(
            "no review ledger record matches the target content; "
            "review the final committed content before emitting v2 evidence"
        )
    if errors:
        raise EvidenceError(f"newest matching review ledger outcome is malformed: {'; '.join(sorted(errors))}")
    if len(statuses) != 1:
        raise EvidenceError(f"newest matching review ledger outcomes disagree: {sorted(statuses)}")
    status = next(iter(statuses))
    if status != "clean":
        raise EvidenceError(f"newest matching review ledger outcome is {status!r}, not 'clean'")
    return selected


def _receipt_payload(
    *,
    repository: str,
    target_content_sha256: str,
    record: LedgerRecord,
    reviewed_commit: str,
) -> dict[str, Any]:
    return {
        "content_profile": CONTENT_PROFILE,
        "kind": RECEIPT_KIND,
        "repository": repository,
        "review": {
            "skill": record.skill,
            "status": "clean",
            "timestamp": record.timestamp_text,
        },
        "reviewed_commit": reviewed_commit,
        "reviewed_content_sha256": target_content_sha256,
        "schema_version": SCHEMA_VERSION,
        "source_record_sha256": record.raw_sha256,
    }


def _read_existing_at(parent_fd: int, filename: str, *, expected_length: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(filename, flags, dir_fd=parent_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceError(f"existing receipt leaf {filename!r} is not a regular file")
        if metadata.st_size != expected_length:
            return b""
        chunks: list[bytes] = []
        remaining = expected_length + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, raw: bytes) -> None:
    position = 0
    while position < len(raw):
        written = os.write(descriptor, raw[position:])
        if written <= 0:
            raise EvidenceError("receipt write made no progress")
        position += written


def _write_exclusive(root: Path, relative_path: Path, raw: bytes) -> bool:
    """Atomically create a repo-relative receipt without following symlinks."""

    if (
        relative_path.is_absolute()
        or len(relative_path.parts) < 2
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise EvidenceError(f"receipt path must be a safe relative path: {relative_path}")
    if not all(hasattr(os, name) for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")):
        raise EvidenceError("safe receipt creation requires POSIX no-follow directory operations")

    rendered_path = root / relative_path
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd: int | None = None
    temporary_name: str | None = None
    try:
        parent_fd = os.open(root, directory_flags)
        for component in relative_path.parts[:-1]:
            try:
                os.mkdir(component, mode=0o755, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
            child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd

        filename = relative_path.parts[-1]
        try:
            existing = _read_existing_at(parent_fd, filename, expected_length=len(raw))
        except FileNotFoundError:
            pass
        else:
            if existing != raw:
                raise EvidenceError(f"refusing to overwrite non-identical receipt {rendered_path}")
            return False

        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        for _ in range(16):
            candidate = f".receipt-{os.getpid()}-{secrets.token_hex(8)}"
            try:
                descriptor = os.open(candidate, write_flags, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        else:
            raise EvidenceError(f"cannot allocate a temporary receipt beside {rendered_path}")

        try:
            _write_all(descriptor, raw)
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        try:
            os.link(
                temporary_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            existing = _read_existing_at(parent_fd, filename, expected_length=len(raw))
            if existing != raw:
                raise EvidenceError(f"concurrent process created non-identical receipt {rendered_path}") from exc
            return False
        os.fsync(parent_fd)
        return True
    except EvidenceError:
        raise
    except OSError as exc:
        raise EvidenceError(f"cannot create receipt {rendered_path}: {exc}") from exc
    finally:
        if parent_fd is not None:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            os.close(parent_fd)


def emit_v2(
    repo: GitRepository,
    *,
    target: str = "HEAD",
    ledger_root: Path | None = None,
) -> tuple[Path, bool]:
    repository = repository_identity(repo)
    initial_head = repo.head()
    target_commit = repo.resolve_commit(target)
    if target_commit != initial_head:
        raise EvidenceError(
            f"emit target {target_commit} is not the checked-out HEAD {initial_head}; "
            "check out the reviewed commit first"
        )
    initial_status = repo.status_bytes()

    target_snapshot = snapshot_tree(repo, target_commit, repository=repository)
    record, reviewed_commit = select_review_record(
        repo,
        target_content_sha256=target_snapshot.content_sha256,
        repository=repository,
        ledger_root=ledger_root,
    )
    payload = _receipt_payload(
        repository=repository,
        target_content_sha256=target_snapshot.content_sha256,
        record=record,
        reviewed_commit=reviewed_commit,
    )
    raw = canonical_json_bytes(payload)
    receipt_digest = hashlib.sha256(raw).hexdigest()
    relative_path = Path(RECEIPT_ROOT) / target_snapshot.content_sha256 / f"{receipt_digest}.json"
    allowed_existing_status = b"? " + os.fsencode(relative_path.as_posix()) + b"\0"
    if initial_status not in {b"", allowed_existing_status}:
        raise EvidenceError(
            "working tree, index, or untracked-file state is dirty; commit or remove every change first"
        )

    synthetic_entry = TreeEntry(
        raw=b"",
        mode=b"100644",
        kind=b"blob",
        oid="0" * repo.oid_length,
        path=os.fsencode(relative_path.as_posix()),
    )
    _validate_receipt(repo=repo, repository=repository, entry=synthetic_entry, raw=raw)

    if repo.head() != initial_head:
        raise EvidenceError("HEAD changed while evidence was being computed; review the new head and retry")
    if repo.status_bytes() not in {b"", allowed_existing_status}:
        raise EvidenceError("working tree changed while evidence was being computed; review the new content and retry")

    created = _write_exclusive(repo.root, relative_path, raw)
    return relative_path, created


def _tree_content_digest(repo: GitRepository, tree_oid: str) -> str:
    """Content digest of a raw tree object under the same profile as snapshot_tree.

    Used only as an equality witness during reattestation. v2 and HA Green
    receipt entries are excluded from the digest entirely — exactly as
    snapshot_tree excludes them — so their blob contents cannot affect the
    equality claim and are not validated here; the surviving path-shape and mode
    checks can only cause a refusal, never a false accept. The head tree that
    actually carries the receipts is separately validated by snapshot_tree.
    """

    digest = hashlib.sha256()
    previous_path: bytes | None = None
    ordinary_entries = 0
    ordinary_bytes = 0
    ha_receipt_count = 0
    for entry in repo.tree_entries_for_tree(tree_oid, max_record_bytes=MAX_TREE_RECORD_BYTES):
        if entry.path == previous_path:
            raise GitError(f"git ls-tree returned duplicate path {_display_path(entry.path)}")
        previous_path = entry.path
        if entry.path == RECEIPT_NAMESPACE_BYTES or entry.path.startswith(RECEIPT_ROOT_BYTES):
            if _RECEIPT_PATH_RE.fullmatch(entry.path) is None:
                raise EvidenceError(f"unknown entry in reserved v2 namespace: {_display_path(entry.path)}")
            if entry.mode != b"100644" or entry.kind != b"blob":
                mode = entry.mode.decode("ascii", errors="replace")
                kind = entry.kind.decode("ascii", errors="replace")
                raise EvidenceError(
                    f"v2 receipt {_display_path(entry.path)} must be a non-executable regular blob, got {mode} {kind}"
                )
            continue
        if _HA_RECEIPT_PATH_RE.fullmatch(entry.path) is not None:
            if entry.mode != b"100644" or entry.kind != b"blob":
                mode = entry.mode.decode("ascii", errors="replace")
                kind = entry.kind.decode("ascii", errors="replace")
                raise EvidenceError(
                    f"HA Green receipt {_display_path(entry.path)} must be a non-executable regular blob, "
                    f"got {mode} {kind}"
                )
            ha_receipt_count += 1
            if ha_receipt_count > MAX_HA_RECEIPTS:
                raise EvidenceError(f"tree contains more than {MAX_HA_RECEIPTS} HA Green receipts")
            continue
        ordinary_entries += 1
        if ordinary_entries > MAX_TREE_ENTRIES:
            raise GitError(f"git ls-tree returned more than {MAX_TREE_ENTRIES} ordinary entries")
        ordinary_bytes += len(entry.raw_with_nul)
        if ordinary_bytes > MAX_TREE_BYTES:
            raise GitError(f"git ls-tree returned more than {MAX_TREE_BYTES} ordinary bytes")
        digest.update(entry.raw_with_nul)
    return digest.hexdigest()


def _retire_superseded_receipts(
    repo: GitRepository,
    *,
    head_snapshot: TreeSnapshot,
    landed_commit: str,
) -> tuple[Path, ...]:
    """Remove branch receipts that no longer bind HEAD's content.

    Retirement is anchored to ``landed_commit`` (a resolved ``origin/main``), NOT
    to any ``--base`` a caller supplied: a receipt is removed only when it (a)
    does not bind the head content and (b) is absent from the landed commit's
    tree. So a receipt already landed on main is never removed, whatever base was
    passed, and a stale or wrong base cannot delete landed evidence. A receipt
    that binds the head content is always kept. Returns the removed paths.
    """

    head_digest = head_snapshot.content_sha256
    removed: list[Path] = []
    for path, receipt in sorted(head_snapshot.receipts.items()):
        if receipt.reviewed_content_sha256 == head_digest:
            continue
        if repo.path_in_commit(landed_commit, path):
            continue
        relative = Path(os.fsdecode(path))
        absolute = repo.root / relative
        try:
            absolute.unlink(missing_ok=True)
        except OSError as exc:
            raise EvidenceError(f"superseded receipt {relative.as_posix()} could not be removed: {exc}") from exc
        removed.append(relative)
        try:
            absolute.parent.rmdir()
        except OSError:
            pass
    return tuple(removed)


def retire_superseded_receipts(
    repo: GitRepository,
    *,
    landed_ref: str = DEFAULT_LANDED_REF,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Snapshot HEAD and retire its now-stale branch receipts.

    Returns ``(removed_paths, blocked_paths)``. ``blocked_paths`` is non-empty
    only when the landed ref does not resolve (an unfetched clone, a detached CI
    checkout) yet stale receipts exist: nothing is removed, and the caller must
    fail loud so the operator fetches and retires by hand rather than shipping a
    branch that CI will reject — never silently deleting a receipt that may be on
    main.
    """

    repository = repository_identity(repo)
    head_snapshot = snapshot_tree(repo, repo.head(), repository=repository, retain_all_receipts=True)
    head_digest = head_snapshot.content_sha256
    stale = [path for path, receipt in head_snapshot.receipts.items() if receipt.reviewed_content_sha256 != head_digest]
    if not stale:
        return (), ()
    try:
        landed_commit = repo.resolve_commit(landed_ref)
    except GitError:
        return (), tuple(sorted(Path(os.fsdecode(path)) for path in stale))
    return _retire_superseded_receipts(repo, head_snapshot=head_snapshot, landed_commit=landed_commit), ()


def reattest_v2(
    repo: GitRepository,
    *,
    base: str,
    target: str = "HEAD",
    landed_ref: str = DEFAULT_LANDED_REF,
) -> tuple[Path, bool, tuple[Path, ...]]:
    """Derive a receipt for reviewed content cleanly merged with the base.

    A base integration (``git merge origin/main``) changes the tree, so the
    existing receipt no longer names HEAD's content even though no unreviewed
    line of this branch changed. This re-issues the receipt without a fresh
    review — but only when git itself proves that HEAD is exactly the reviewed
    content three-way-merged with the base and nothing else: a conflicted merge,
    a hand-edited merge commit, or any commit that touches ordinary content
    after the review all fail closed into a fresh squad run.

    The base must be landed, trusted content — contained in the landed ref
    (``origin/main`` by default). Only then is the three-way merge sound: every
    line of the merge result comes from the reviewed side or the trusted base, so
    an untrusted base (``--base HEAD``, ``--base <an-unmerged-feature-branch>``)
    that could smuggle in unreviewed content is refused before the witness runs.

    After writing the derived receipt, this retires the branch's now-stale
    receipts (see ``_retire_superseded_receipts``) so the caller can commit the
    swap together — the automated form of the manual "retire stale preship
    receipt" step. Retirement is anchored to the landed ref, never to ``base``,
    so no landed receipt is ever removed. The derivation stays reconstructible
    from history: the source receipt remains reachable in prior commits.

    Returns ``(receipt_path, created, superseded_paths)``.
    """

    repository = repository_identity(repo)
    initial_head = repo.head()
    target_commit = repo.resolve_commit(target)
    if target_commit != initial_head:
        raise EvidenceError(
            f"reattest target {target_commit} is not the checked-out HEAD {initial_head}; "
            "check out the integrated commit first"
        )
    base_commit = repo.resolve_commit(base)
    if not repo.is_ancestor(base_commit, initial_head):
        raise EvidenceError(
            f"base {base_commit} is not an ancestor of HEAD; integrate the base first (git merge <base>), then reattest"
        )
    # The witness base must be landed/trusted content. Without this a divergent
    # but untrusted base (a feature branch merged into HEAD, then named as the
    # base) would let the sound-looking three-way merge sign unreviewed content.
    try:
        landed_commit = repo.resolve_commit(landed_ref)
    except GitError as exc:
        raise EvidenceError(
            f"cannot verify the reattest base: {landed_ref!r} does not resolve — fetch it and retry"
        ) from exc
    if not repo.is_ancestor(base_commit, landed_commit):
        raise EvidenceError(
            f"reattest base {base_commit} is not landed content in {landed_ref!r}; integrate the landed base "
            "(git merge origin/main) and reattest against it, never against an unmerged branch"
        )
    initial_status = repo.status_bytes()

    head_snapshot = snapshot_tree(repo, initial_head, repository=repository, retain_all_receipts=True)
    already_matching = _matching_receipts(head_snapshot)
    if already_matching:
        # A receipt already binds head content; nothing to derive. Hold the same
        # clean-tree bar as the create path so a re-run cannot silently succeed on
        # a dirty tree, then retire any stale receipt left tracked by a partial
        # commit — the short-circuit must not skip retirement.
        if initial_status != b"":
            raise EvidenceError(
                "working tree, index, or untracked-file state is dirty; commit or remove every change first"
            )
        chosen = min(already_matching, key=lambda receipt: receipt.path)
        superseded = _retire_superseded_receipts(repo, head_snapshot=head_snapshot, landed_commit=landed_commit)
        return Path(os.fsdecode(chosen.path)), False, superseded
    if not head_snapshot.receipts:
        raise EvidenceError(
            "no v2 receipt exists on this branch to derive from; run the review squad and emit a fresh receipt instead"
        )

    reasons: list[str] = []
    source: Receipt | None = None
    # Deterministic candidate order; every qualifying candidate derives the same
    # content digest, so path order only decides which review metadata is carried.
    for receipt in sorted(head_snapshot.receipts.values(), key=lambda entry: entry.path):
        label = _display_path(receipt.path)
        try:
            reviewed_commit = repo.resolve_full_commit(receipt.reviewed_commit)
        except GitError:
            reasons.append(f"{label} pins an unavailable reviewed commit")
            continue
        if not repo.is_ancestor(reviewed_commit, initial_head):
            reasons.append(f"{label} pins a reviewed commit outside this branch's history")
            continue
        if (
            snapshot_tree(repo, reviewed_commit, repository=repository).content_sha256
            != receipt.reviewed_content_sha256
        ):
            reasons.append(f"{label} does not match its reviewed commit's content")
            continue
        merged_tree = repo.merge_tree_commits(reviewed_commit, base_commit)
        if merged_tree is None:
            reasons.append(f"{label}: merging its reviewed commit with the base conflicts")
            continue
        if _tree_content_digest(repo, merged_tree) != head_snapshot.content_sha256:
            reasons.append(f"{label}: HEAD is not exactly its reviewed content merged with the base")
            continue
        source = receipt
        break
    if source is None:
        raise EvidenceError(
            "no existing receipt derives this content ("
            + "; ".join(reasons)
            + "); changed content needs a fresh review squad run and a fresh receipt"
        )

    source_payload = _decode_json_object(
        repo.run(("cat-file", "blob", f"{initial_head}:{os.fsdecode(source.path)}")),
        label=f"source receipt {_display_path(source.path)}",
    )
    source_review = source_payload["review"]
    payload = {
        "content_profile": CONTENT_PROFILE,
        "kind": RECEIPT_KIND,
        "repository": repository,
        "review": {
            "skill": source_review["skill"],
            "status": "clean",
            "timestamp": source_review["timestamp"],
        },
        "reviewed_commit": initial_head,
        "reviewed_content_sha256": head_snapshot.content_sha256,
        "schema_version": SCHEMA_VERSION,
        "source_record_sha256": source_payload["source_record_sha256"],
    }
    raw = canonical_json_bytes(payload)
    receipt_digest = hashlib.sha256(raw).hexdigest()
    relative_path = Path(RECEIPT_ROOT) / head_snapshot.content_sha256 / f"{receipt_digest}.json"
    allowed_existing_status = b"? " + os.fsencode(relative_path.as_posix()) + b"\0"
    if initial_status not in {b"", allowed_existing_status}:
        raise EvidenceError(
            "working tree, index, or untracked-file state is dirty; commit or remove every change first"
        )

    synthetic_entry = TreeEntry(
        raw=b"",
        mode=b"100644",
        kind=b"blob",
        oid="0" * repo.oid_length,
        path=os.fsencode(relative_path.as_posix()),
    )
    _validate_receipt(repo=repo, repository=repository, entry=synthetic_entry, raw=raw)

    if repo.head() != initial_head:
        raise EvidenceError("HEAD changed while evidence was being computed; review the new head and retry")
    if repo.status_bytes() not in {b"", allowed_existing_status}:
        raise EvidenceError("working tree changed while evidence was being computed; review the new content and retry")

    created = _write_exclusive(repo.root, relative_path, raw)

    # Retire the branch's now-stale receipts strictly after the derived receipt is
    # durably written, so an interrupted run never leaves the branch with less
    # evidence than it had. The new receipt is not in head_snapshot (it is still
    # untracked), and it binds the head content, so retirement never touches it.
    # landed_commit was resolved up front for the base-trust check, so retirement
    # here never needs to handle an unresolvable landed ref.
    superseded = _retire_superseded_receipts(repo, head_snapshot=head_snapshot, landed_commit=landed_commit)

    return relative_path, created, superseded


def _matching_receipts(snapshot: TreeSnapshot) -> list[Receipt]:
    return [
        receipt
        for receipt in snapshot.receipts.values()
        if receipt.reviewed_content_sha256 == snapshot.content_sha256
        and (receipt.content_profile == CONTENT_PROFILE or not snapshot.has_ha_green_receipts)
    ]


def verify_v2(
    repo: GitRepository,
    *,
    target: str,
    base: str | None,
    mode: str,
) -> VerificationResult:
    if mode not in {"pr", "main"}:
        raise EvidenceError(f"unsupported verification mode {mode!r}")
    repository = repository_identity(repo)
    target_commit = repo.resolve_commit(target)

    if mode == "pr":
        if not base:
            raise EvidenceError("PR verification requires an explicit base commit")
        base_commit = repo.resolve_commit(base)
        if not repo.is_ancestor(base_commit, target_commit):
            raise EvidenceError(
                f"base {base_commit} is not an ancestor of target {target_commit}; update the branch and review again"
            )
        added_paths = repo.diff_paths(
            base_commit,
            target_commit,
            pathspec=RECEIPT_ROOT,
            diff_filter="A",
            max_paths=MAX_NEW_RECEIPTS,
            max_path_bytes=MAX_RECEIPT_PATH_BYTES,
        )
        if len(added_paths) > MAX_NEW_RECEIPTS:
            raise EvidenceError(f"PR adds more than {MAX_NEW_RECEIPTS} entries in the reserved v2 namespace")
        changed_base_paths = repo.diff_paths(
            base_commit,
            target_commit,
            pathspec=RECEIPT_ROOT,
            diff_filter="a",
            max_paths=0,
            max_path_bytes=MAX_RECEIPT_PATH_BYTES,
        )
        if changed_base_paths:
            raise EvidenceError(f"base v2 receipt {_display_path(changed_base_paths[0])} was deleted or modified")
        target_snapshot = snapshot_tree(
            repo,
            target_commit,
            repository=repository,
            receipt_paths=frozenset(added_paths),
        )

        new_receipts = list(target_snapshot.receipts.values())
        if not new_receipts:
            raise EvidenceError("PR adds no new v2 review receipt")

        digest_cache: dict[str, str] = {}
        for receipt in new_receipts:
            if receipt.content_profile != CONTENT_PROFILE:
                raise EvidenceError(
                    f"new v2 receipt {_display_path(receipt.path)} must use content profile {CONTENT_PROFILE!r}"
                )
            try:
                reviewed_commit = repo.resolve_full_commit(receipt.reviewed_commit)
            except GitError as exc:
                raise EvidenceError(
                    f"new v2 receipt {_display_path(receipt.path)} pins an unavailable reviewed commit"
                ) from exc
            if reviewed_commit not in digest_cache:
                if not repo.is_ancestor(base_commit, reviewed_commit) or not repo.is_ancestor(
                    reviewed_commit, target_commit
                ):
                    raise EvidenceError(
                        f"new v2 receipt {_display_path(receipt.path)} pins a reviewed commit "
                        "outside base-to-target history"
                    )
                reviewed_added_paths = repo.diff_paths(
                    base_commit,
                    reviewed_commit,
                    pathspec=RECEIPT_ROOT,
                    diff_filter="A",
                    max_paths=MAX_NEW_RECEIPTS,
                    max_path_bytes=MAX_RECEIPT_PATH_BYTES,
                )
                if len(reviewed_added_paths) > MAX_NEW_RECEIPTS:
                    raise EvidenceError(
                        f"reviewed commit {reviewed_commit} adds more than {MAX_NEW_RECEIPTS} entries "
                        "in the reserved v2 namespace"
                    )
                reviewed_changed_paths = repo.diff_paths(
                    base_commit,
                    reviewed_commit,
                    pathspec=RECEIPT_ROOT,
                    diff_filter="a",
                    max_paths=0,
                    max_path_bytes=MAX_RECEIPT_PATH_BYTES,
                )
                if reviewed_changed_paths:
                    raise EvidenceError(
                        f"reviewed commit {reviewed_commit} deletes or modifies base v2 receipt "
                        f"{_display_path(reviewed_changed_paths[0])}"
                    )
                digest_cache[reviewed_commit] = snapshot_tree(
                    repo,
                    reviewed_commit,
                    repository=repository,
                ).content_sha256
            if digest_cache[reviewed_commit] != receipt.reviewed_content_sha256:
                raise EvidenceError(
                    f"new v2 receipt {_display_path(receipt.path)} does not match its reviewed commit's content"
                )
            if receipt.reviewed_content_sha256 != target_snapshot.content_sha256:
                raise EvidenceError(f"new v2 receipt {_display_path(receipt.path)} does not match the target content")

        matching = new_receipts
    else:
        target_snapshot = snapshot_tree(
            repo,
            target_commit,
            repository=repository,
            retain_matching_receipts=True,
        )
        matching = _matching_receipts(target_snapshot)
        if not matching:
            raise EvidenceError("no surviving v2 receipt matches the main-tree content")

    return VerificationResult(
        mode=mode,
        target=target_commit,
        content_sha256=target_snapshot.content_sha256,
        matching_receipts=tuple(sorted(os.fsdecode(receipt.path) for receipt in matching)),
    )

#!/usr/bin/env python3
"""Promote one explicitly approved complete-audio candidate atomically.

The listening board and the runtime pack are deliberately separate.  The
caller supplies the board ``manifest.json`` and its expected content digest;
the board pins one immutable ``pack/manifest.json``.  This command validates
that exact projection, stages ``pack/`` beside the target, validates the staged
runtime pack, and swaps directories without restarting the radio.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

try:
    _gate = importlib.import_module("scripts.complete_audio_pack_gate")
    _validator = importlib.import_module("scripts.validate_audio_asset_pack")
except ModuleNotFoundError as exc:  # Direct ``python scripts/...`` invocation.
    if exc.name != "scripts":
        raise
    _gate = importlib.import_module("complete_audio_pack_gate")
    _validator = importlib.import_module("validate_audio_asset_pack")


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = REPO_ROOT / "mammamiradio" / "assets" / "imaging"
RUNTIME_PROJECTION_FIELDS = frozenset({"path", "manifest_sha256", "pack_digest", "inventory_digest"})
RUNTIME_MANIFEST_PATH = "pack/manifest.json"
INVENTORY_SCHEMA_VERSION = 1
INVENTORY_FIELDS = frozenset({"schema_version", "files", "digest"})
INVENTORY_FILE_FIELDS = frozenset({"path", "sha256"})
RUNTIME_CONTROL_FILES = frozenset({"README.md", "ATTRIBUTION.md"})
BOARD_ONLY_RUNTIME_FIELDS = frozenset(
    {
        "board_files",
        "content_digest",
        "install_projection",
        "listening_receipt",
        "previews",
        "release_ready",
        "runtime_projection",
    }
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PromotionError(ValueError):
    """The requested candidate cannot safely replace the runtime pack."""


@dataclass(frozen=True)
class InventoryFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class RuntimeProjection:
    candidate_pack: Path
    manifest_sha256: str
    pack_digest: str
    inventory_digest: str
    files: tuple[InventoryFile, ...]

    @property
    def expected_files(self) -> dict[str, str]:
        return {
            "manifest.json": self.manifest_sha256,
            **{item.path: item.sha256 for item in self.files},
        }


@dataclass(frozen=True)
class PromotionResult:
    target: Path
    content_digest: str
    pack_digest: str
    inventory_digest: str
    changed: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise PromotionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _relative_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PromotionError(f"{label} must be a canonical relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise PromotionError(f"{label} must be a canonical relative POSIX path")
    if any(part.startswith(".") for part in path.parts):
        raise PromotionError(f"{label} must not contain hidden path components")
    if path.parts[0] in {"previews", "tmp"}:
        raise PromotionError(f"{label} must not select board or temporary material")
    return value


def _inventory_digest(files: Sequence[InventoryFile | Mapping[str, object]]) -> str:
    """Hash a path/checksum ledger using the audio-pack digest framing."""
    records: list[tuple[str, str]] = []
    for index, item in enumerate(files):
        if isinstance(item, InventoryFile):
            path, checksum = item.path, item.sha256
        elif isinstance(item, Mapping):
            path = _relative_path(item.get("path"), label=f"inventory.files[{index}].path")
            checksum = _require_sha256(item.get("sha256"), label=f"inventory.files[{index}].sha256")
        else:
            raise PromotionError(f"inventory.files[{index}] must be an object")
        records.append((path, checksum))
    digest = hashlib.sha256()
    for path, checksum in sorted(records):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(checksum.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"{label} is not readable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"{label} root must be an object")
    return value


def _regular_file(root: Path, relative: str, *, label: str) -> Path:
    relative = _relative_path(relative, label=label)
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError as exc:
            raise PromotionError(f"{label} is missing or unreadable: {relative}") from exc
        if stat.S_ISLNK(mode):
            raise PromotionError(f"{label} must not traverse a symlink: {relative}")
        final = index == len(parts) - 1
        if final and not stat.S_ISREG(mode):
            raise PromotionError(f"{label} must be a regular file: {relative}")
        if not final and not stat.S_ISDIR(mode):
            raise PromotionError(f"{label} has a non-directory parent: {relative}")
    return current


def _tree_files(root: Path, *, label: str) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise PromotionError(f"{label} must be a real directory: {root}")
    files: dict[str, str] = {}

    def visit(directory: Path, relative_parent: PurePosixPath) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise PromotionError(f"{label} is unreadable: {directory}") from exc
        for entry in entries:
            relative_path = relative_parent / entry.name
            relative = relative_path.as_posix()
            try:
                if entry.is_symlink():
                    raise PromotionError(f"{label} contains a symlink: {relative}")
                if entry.is_dir(follow_symlinks=False):
                    visit(Path(entry.path), relative_path)
                elif entry.is_file(follow_symlinks=False):
                    files[relative] = _sha256(Path(entry.path))
                else:
                    raise PromotionError(f"{label} contains a special filesystem entry: {relative}")
            except OSError as exc:
                raise PromotionError(f"{label} changed while it was inspected: {relative}") from exc

    visit(root, PurePosixPath())
    return files


def _parse_inventory(runtime_manifest: Mapping[str, object]) -> tuple[tuple[InventoryFile, ...], str]:
    forbidden = sorted(BOARD_ONLY_RUNTIME_FIELDS & runtime_manifest.keys())
    if forbidden:
        raise PromotionError(f"Runtime manifest contains board-only fields: {', '.join(forbidden)}")
    raw_inventory = runtime_manifest.get("inventory")
    if not isinstance(raw_inventory, Mapping) or set(raw_inventory) != INVENTORY_FIELDS:
        raise PromotionError("Runtime manifest has no exact schema-v1 inventory")
    schema_version = raw_inventory.get("schema_version")
    if type(schema_version) is not int or schema_version != INVENTORY_SCHEMA_VERSION:
        raise PromotionError("Runtime inventory has the wrong schema version")
    raw_files = raw_inventory.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise PromotionError("Runtime inventory must contain files")
    files: list[InventoryFile] = []
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, Mapping) or set(raw_file) != INVENTORY_FILE_FIELDS:
            raise PromotionError(f"Runtime inventory file {index} has an invalid field set")
        relative = _relative_path(raw_file.get("path"), label=f"inventory.files[{index}].path")
        if relative == "manifest.json":
            raise PromotionError("Runtime inventory must exclude its own manifest")
        checksum = _require_sha256(raw_file.get("sha256"), label=f"inventory.files[{index}].sha256")
        files.append(InventoryFile(relative, checksum))
    paths = [item.path for item in files]
    if len(paths) != len(set(paths)):
        raise PromotionError("Runtime inventory contains duplicate paths")
    if paths != sorted(paths):
        raise PromotionError("Runtime inventory files must be sorted by path")
    declared_digest = _require_sha256(raw_inventory.get("digest"), label="inventory.digest")
    expected_digest = _inventory_digest(files)
    if declared_digest != expected_digest:
        raise PromotionError("Runtime inventory digest is stale")

    raw_assets = runtime_manifest.get("assets")
    raw_sources = runtime_manifest.get("sources")
    if not isinstance(raw_assets, list) or not raw_assets or not isinstance(raw_sources, list) or not raw_sources:
        raise PromotionError("Runtime manifest must retain non-empty asset and source inventories")
    declared_files: dict[str, str] = {}
    for control in RUNTIME_CONTROL_FILES:
        declared_files[control] = next((item.sha256 for item in files if item.path == control), "")
    if any(not checksum for checksum in declared_files.values()):
        raise PromotionError("Runtime inventory must include README.md and ATTRIBUTION.md")
    for index, raw_asset in enumerate(raw_assets):
        if not isinstance(raw_asset, Mapping):
            raise PromotionError(f"Runtime asset {index} must be an object")
        path = _relative_path(raw_asset.get("path"), label=f"assets[{index}].path")
        checksum = _require_sha256(raw_asset.get("sha256"), label=f"assets[{index}].sha256")
        if path in declared_files:
            raise PromotionError(f"Runtime delivery path repeats: {path}")
        declared_files[path] = checksum
    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, Mapping):
            raise PromotionError(f"Runtime source {index} must be an object")
        path = _relative_path(raw_source.get("original_path"), label=f"sources[{index}].original_path")
        checksum = _require_sha256(raw_source.get("source_sha256"), label=f"sources[{index}].source_sha256")
        if path in declared_files:
            raise PromotionError(f"Runtime delivery path repeats: {path}")
        declared_files[path] = checksum
    inventory_files = {item.path: item.sha256 for item in files}
    if inventory_files != declared_files:
        missing = sorted(set(declared_files) - set(inventory_files))
        extra = sorted(set(inventory_files) - set(declared_files))
        raise PromotionError(
            f"Runtime inventory differs from controls/assets/masters (missing={missing}, extra={extra})"
        )
    for path, checksum in declared_files.items():
        if inventory_files[path] != checksum:
            raise PromotionError(f"Runtime inventory checksum differs from manifest metadata: {path}")
    return tuple(files), declared_digest


def _load_projection(board_manifest_path: Path, expected_content_digest: str) -> RuntimeProjection:
    if board_manifest_path.is_symlink():
        raise PromotionError("Complete-pack board manifest must not be a symlink")
    try:
        board_manifest_path = board_manifest_path.resolve(strict=True)
    except OSError as exc:
        raise PromotionError(f"Complete-pack board manifest is missing: {board_manifest_path}") from exc
    board = _read_json(board_manifest_path, label="Complete-pack board manifest")
    if board.get("content_digest") != expected_content_digest:
        raise PromotionError("Complete-pack board manifest has the wrong content digest")
    raw_projection = board.get("runtime_projection")
    if not isinstance(raw_projection, Mapping) or set(raw_projection) != RUNTIME_PROJECTION_FIELDS:
        raise PromotionError("Complete-pack board has no exact runtime_projection contract")
    if raw_projection.get("path") != RUNTIME_MANIFEST_PATH:
        raise PromotionError(f"Runtime projection path must be {RUNTIME_MANIFEST_PATH}")
    manifest_sha256 = _require_sha256(raw_projection.get("manifest_sha256"), label="runtime_projection.manifest_sha256")
    pack_digest = _require_sha256(raw_projection.get("pack_digest"), label="runtime_projection.pack_digest")
    inventory_digest = _require_sha256(
        raw_projection.get("inventory_digest"), label="runtime_projection.inventory_digest"
    )
    run_root = board_manifest_path.parent
    runtime_manifest_path = _regular_file(
        run_root,
        RUNTIME_MANIFEST_PATH,
        label="runtime projection manifest",
    )
    if _sha256(runtime_manifest_path) != manifest_sha256:
        raise PromotionError("Runtime projection manifest hash is stale")
    runtime_manifest = _read_json(runtime_manifest_path, label="Runtime manifest")
    if runtime_manifest.get("pack_digest") != pack_digest:
        raise PromotionError("Runtime projection pack digest differs from the runtime manifest")
    files, actual_inventory_digest = _parse_inventory(runtime_manifest)
    if actual_inventory_digest != inventory_digest:
        raise PromotionError("Runtime projection inventory digest differs from the runtime manifest")
    return RuntimeProjection(
        candidate_pack=runtime_manifest_path.parent,
        manifest_sha256=manifest_sha256,
        pack_digest=pack_digest,
        inventory_digest=inventory_digest,
        files=files,
    )


def _validate_runtime_pack(pack_dir: Path, projection: RuntimeProjection) -> dict[str, str]:
    actual_files = _tree_files(pack_dir, label="Runtime pack")
    if actual_files != projection.expected_files:
        missing = sorted(set(projection.expected_files) - set(actual_files))
        extra = sorted(set(actual_files) - set(projection.expected_files))
        changed = sorted(
            path
            for path in set(actual_files) & set(projection.expected_files)
            if actual_files[path] != projection.expected_files[path]
        )
        raise PromotionError(f"Runtime pack inventory differs (missing={missing}, extra={extra}, changed={changed})")
    runtime_manifest = _read_json(pack_dir / "manifest.json", label="Runtime manifest")
    files, inventory_digest = _parse_inventory(runtime_manifest)
    if runtime_manifest.get("pack_digest") != projection.pack_digest:
        raise PromotionError("Runtime pack has the wrong pack digest")
    if files != projection.files or inventory_digest != projection.inventory_digest:
        raise PromotionError("Runtime pack inventory differs from the approved projection")
    try:
        report = _validator.validate_audio_asset_pack(pack_dir)
        _validator.check_attribution(report)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PromotionError(f"Runtime pack validation failed: {exc}") from exc
    return actual_files


def _legacy_declared_files(target: Path, actual_files: Mapping[str, str]) -> dict[str, str]:
    manifest = _read_json(target / "manifest.json", label="Installed runtime manifest")
    raw_assets = manifest.get("assets")
    raw_sources = manifest.get("sources")
    if not isinstance(raw_assets, list) or not raw_assets or not isinstance(raw_sources, list) or not raw_sources:
        raise PromotionError("Installed runtime manifest has no asset/source inventory")
    expected = {
        "manifest.json": actual_files.get("manifest.json", ""),
        "README.md": actual_files.get("README.md", ""),
        "ATTRIBUTION.md": actual_files.get("ATTRIBUTION.md", ""),
    }
    if any(not checksum for checksum in expected.values()):
        raise PromotionError("Installed runtime pack is missing its control files")
    for index, raw_asset in enumerate(raw_assets):
        if not isinstance(raw_asset, Mapping):
            raise PromotionError(f"Installed asset {index} must be an object")
        path = _relative_path(raw_asset.get("path"), label=f"installed assets[{index}].path")
        checksum = _require_sha256(raw_asset.get("sha256"), label=f"installed assets[{index}].sha256")
        if path in expected:
            raise PromotionError(f"Installed delivery path repeats: {path}")
        expected[path] = checksum
    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, Mapping) or "original_path" not in raw_source:
            continue
        path = _relative_path(raw_source.get("original_path"), label=f"installed sources[{index}].original_path")
        checksum = _require_sha256(raw_source.get("source_sha256"), label=f"installed sources[{index}].source_sha256")
        if path in expected:
            raise PromotionError(f"Installed delivery path repeats: {path}")
        expected[path] = checksum
    return expected


def _assert_git_clean(target: Path) -> None:
    try:
        relative = target.relative_to(REPO_ROOT)
    except ValueError:
        return
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", relative.as_posix()],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PromotionError("Cannot establish whether the installed target is clean")
    if result.stdout:
        raise PromotionError("Installed target has uncommitted or untracked files")


def _assert_existing_target_clean(target: Path) -> dict[str, str]:
    actual_files = _tree_files(target, label="Installed target")
    runtime_manifest = _read_json(target / "manifest.json", label="Installed runtime manifest")
    if "inventory" in runtime_manifest:
        files, inventory_digest = _parse_inventory(runtime_manifest)
        manifest_sha256 = actual_files.get("manifest.json")
        pack_digest = _require_sha256(runtime_manifest.get("pack_digest"), label="installed pack_digest")
        if not manifest_sha256:
            raise PromotionError("Installed target has no runtime manifest")
        current_projection = RuntimeProjection(
            candidate_pack=target,
            manifest_sha256=manifest_sha256,
            pack_digest=pack_digest,
            inventory_digest=inventory_digest,
            files=files,
        )
        _validate_runtime_pack(target, current_projection)
    else:
        expected = _legacy_declared_files(target, actual_files)
        if actual_files != expected:
            missing = sorted(set(expected) - set(actual_files))
            extra = sorted(set(actual_files) - set(expected))
            changed = sorted(path for path in set(actual_files) & set(expected) if actual_files[path] != expected[path])
            raise PromotionError(
                f"Installed target is dirty or unexpected (missing={missing}, extra={extra}, changed={changed})"
            )
        try:
            report = _validator.validate_audio_asset_pack(target)
            _validator.check_attribution(report)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PromotionError(f"Installed target validation failed: {exc}") from exc
    _assert_git_clean(target)
    return actual_files


def _copy_projection(projection: RuntimeProjection, stage: Path) -> None:
    for relative in projection.expected_files:
        source = _regular_file(projection.candidate_pack, relative, label="candidate runtime file")
        destination = stage.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.complete-audio-pack.lock"


def _rollback_swap(
    *,
    target: Path,
    backup: Path,
    failed_install: Path,
    original_existed: bool,
) -> None:
    if target.exists() or target.is_symlink():
        os.replace(target, failed_install)
    if original_existed:
        if not backup.exists():
            raise RuntimeError("Promotion rollback lost the original target")
        os.replace(backup, target)
    if failed_install.exists():
        shutil.rmtree(failed_install)


def promote_complete_audio_pack(
    board_manifest_path: Path,
    expected_content_digest: str,
    *,
    target: Path = DEFAULT_TARGET,
    failure_injector: Callable[[str], None] | None = None,
) -> PromotionResult:
    """Install one digest-named approved candidate without restarting runtime."""
    expected_content_digest = _require_sha256(expected_content_digest, label="expected content digest")

    # This must remain the first candidate operation: no target, lock, or stage
    # mutation is allowed before the human listening gate passes.
    approved_digest = _gate.assert_complete_listening_gate(board_manifest_path)
    if approved_digest != expected_content_digest:
        raise PromotionError("Approved complete-pack digest does not match the expected content digest")
    projection = _load_projection(board_manifest_path, expected_content_digest)
    _validate_runtime_pack(projection.candidate_pack, projection)

    if target.name in {"", ".", ".."}:
        raise PromotionError(f"Unsafe promotion target: {target}")
    try:
        target_parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise PromotionError(f"Promotion target parent does not exist: {target.parent}") from exc
    target = target_parent / target.name
    if target in (target_parent, REPO_ROOT, projection.candidate_pack):
        raise PromotionError(f"Unsafe promotion target: {target}")
    if target in projection.candidate_pack.parents or projection.candidate_pack in target.parents:
        raise PromotionError("Promotion target must not overlap the candidate pack")
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise PromotionError("Promotion target must be a real directory or absent")

    lock_path = _lock_path(target)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PromotionError(f"Another audio-pack promotion holds the lock: {lock_path}") from exc
    stage = target.parent / f".{target.name}.{uuid4().hex}.promotion-stage"
    backup = target.parent / f".{target.name}.{uuid4().hex}.promotion-rollback"
    failed_install = target.parent / f".{target.name}.{uuid4().hex}.promotion-failed"
    original_existed = target.exists()
    swapped = False
    stage_owned = False
    try:
        if original_existed:
            actual_target = _tree_files(target, label="Installed target")
            if actual_target == projection.expected_files:
                _validate_runtime_pack(target, projection)
                return PromotionResult(
                    target=target,
                    content_digest=expected_content_digest,
                    pack_digest=projection.pack_digest,
                    inventory_digest=projection.inventory_digest,
                    changed=False,
                )
            original_snapshot = _assert_existing_target_clean(target)
        else:
            original_snapshot = None

        stage.mkdir(mode=0o755)
        stage_owned = True
        _copy_projection(projection, stage)
        _validate_runtime_pack(stage, projection)

        # Recheck both immutable inputs and the target immediately before the
        # first directory rename.  This closes races during staging.
        if _gate.assert_complete_listening_gate(board_manifest_path) != expected_content_digest:
            raise PromotionError("Complete-pack listening approval changed during promotion")
        refreshed = _load_projection(board_manifest_path, expected_content_digest)
        if refreshed != projection:
            raise PromotionError("Complete-pack runtime projection changed during promotion")
        _validate_runtime_pack(projection.candidate_pack, projection)
        if original_snapshot is not None and _tree_files(target, label="Installed target") != original_snapshot:
            raise PromotionError("Installed target changed during promotion staging")
        if original_snapshot is None and target.exists():
            raise PromotionError("Promotion target appeared during promotion staging")

        if original_existed:
            os.replace(target, backup)
        if failure_injector is not None:
            failure_injector("after-backup")
        os.replace(stage, target)
        stage_owned = False
        swapped = True
        if failure_injector is not None:
            failure_injector("after-swap")
        _validate_runtime_pack(target, projection)
        if failure_injector is not None:
            failure_injector("after-validation")
    except BaseException:
        if swapped or (original_existed and backup.exists()):
            _rollback_swap(
                target=target,
                backup=backup,
                failed_install=failed_install,
                original_existed=original_existed,
            )
        raise
    finally:
        try:
            if stage_owned and stage.exists():
                shutil.rmtree(stage)
        finally:
            try:
                os.close(lock_fd)
            finally:
                lock_path.unlink(missing_ok=True)

    if backup.exists():
        shutil.rmtree(backup)
    return PromotionResult(
        target=target,
        content_digest=expected_content_digest,
        pack_digest=projection.pack_digest,
        inventory_digest=projection.inventory_digest,
        changed=True,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Exact complete-pack board manifest")
    parser.add_argument("--expected-content-digest", required=True, help="Digest approved by the listening receipt")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="Runtime imaging directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = promote_complete_audio_pack(
            args.manifest,
            args.expected_content_digest,
            target=args.target,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    action = "Promoted" if result.changed else "Already installed"
    print(f"{action}: {result.target}")
    print(f"Content digest: {result.content_digest}")
    print(f"Pack digest: {result.pack_digest}")
    print(f"Inventory digest: {result.inventory_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

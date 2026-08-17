"""Focused safety tests for complete audio-pack promotion."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import promote_complete_audio_pack as promoter

CONTENT_DIGEST = "c" * 64
OTHER_CONTENT_DIGEST = "d" * 64


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _raw_inventory_digest(files: list[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: (value["path"], value["sha256"])):
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass
class Candidate:
    root: Path
    board_manifest: Path
    pack: Path
    asset: Path
    master: Path

    def board(self) -> dict[str, Any]:
        return json.loads(self.board_manifest.read_text(encoding="utf-8"))

    def runtime_manifest(self) -> dict[str, Any]:
        return json.loads((self.pack / "manifest.json").read_text(encoding="utf-8"))

    def save_runtime_manifest(self, runtime: dict[str, Any]) -> None:
        (self.pack / "manifest.json").write_text(
            json.dumps(runtime, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        board = self.board()
        board["runtime_projection"] = {
            "path": promoter.RUNTIME_MANIFEST_PATH,
            "manifest_sha256": _sha256(self.pack / "manifest.json"),
            "pack_digest": runtime["pack_digest"],
            "inventory_digest": runtime["inventory"]["digest"],
        }
        self.board_manifest.write_text(json.dumps(board, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_runtime_pack(pack: Path, marker: str) -> dict[str, Any]:
    readme = _write(pack / "README.md", f"runtime {marker}\n".encode())
    attribution = _write(pack / "ATTRIBUTION.md", f"attribution {marker}\n".encode())
    asset = _write(pack / "audio" / "station.mp3", f"asset::{marker}".encode())
    master = _write(pack / "provenance" / "source-masters" / "station.mp3", f"asset::{marker}".encode())
    files = sorted(
        [
            {"path": "ATTRIBUTION.md", "sha256": _sha256(attribution)},
            {"path": "README.md", "sha256": _sha256(readme)},
            {"path": "audio/station.mp3", "sha256": _sha256(asset)},
            {"path": "provenance/source-masters/station.mp3", "sha256": _sha256(master)},
        ],
        key=lambda item: item["path"],
    )
    runtime = {
        "schema_version": 2,
        "pack_digest": hashlib.sha256(f"pack::{marker}".encode()).hexdigest(),
        "sources": [
            {
                "id": "project.station",
                "original_path": "provenance/source-masters/station.mp3",
                "source_sha256": _sha256(master),
            }
        ],
        "assets": [
            {
                "id": "identity.station",
                "path": "audio/station.mp3",
                "sha256": _sha256(asset),
            }
        ],
        "recipes": [],
        "inventory": {
            "schema_version": promoter.INVENTORY_SCHEMA_VERSION,
            "files": files,
            "digest": _raw_inventory_digest(files),
        },
    }
    (pack / "manifest.json").write_text(json.dumps(runtime, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return runtime


def _candidate(tmp_path: Path, *, status: str = "approved", current: bool = True) -> Candidate:
    root = tmp_path / "candidate"
    pack = root / "pack"
    runtime = _write_runtime_pack(pack, "candidate")
    board = {
        "content_digest": CONTENT_DIGEST,
        "current": current,
        "listening_receipt": {"status": status, "content_digest": CONTENT_DIGEST},
        "runtime_projection": {
            "path": promoter.RUNTIME_MANIFEST_PATH,
            "manifest_sha256": _sha256(pack / "manifest.json"),
            "pack_digest": runtime["pack_digest"],
            "inventory_digest": runtime["inventory"]["digest"],
        },
    }
    board_manifest = root / "manifest.json"
    board_manifest.write_text(json.dumps(board, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return Candidate(
        root=root,
        board_manifest=board_manifest,
        pack=pack,
        asset=pack / "audio" / "station.mp3",
        master=pack / "provenance" / "source-masters" / "station.mp3",
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _promotion_artifacts(target: Path) -> list[Path]:
    return sorted(target.parent.glob(f".{target.name}.*promotion*")) + [
        path for path in [promoter._lock_path(target)] if path.exists()
    ]


@pytest.fixture(autouse=True)
def _safe_dependencies(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Path]]:
    calls: dict[str, list[Path]] = {"gate": [], "validate": [], "attribution": []}

    def assert_gate(path: Path) -> str:
        calls["gate"].append(Path(path))
        board = json.loads(Path(path).read_text(encoding="utf-8"))
        receipt = board.get("listening_receipt", {})
        if not board.get("current"):
            raise ValueError("candidate is non-current")
        if receipt.get("status") != "approved":
            raise ValueError(f"candidate is {receipt.get('status')}")
        if receipt.get("content_digest") != board.get("content_digest"):
            raise ValueError("candidate receipt is stale")
        return str(board["content_digest"])

    def validate(pack_dir: Path) -> SimpleNamespace:
        calls["validate"].append(Path(pack_dir))
        return SimpleNamespace(attribution_path=Path(pack_dir) / "ATTRIBUTION.md")

    def check_attribution(report: SimpleNamespace) -> None:
        calls["attribution"].append(report.attribution_path)

    monkeypatch.setattr(promoter._gate, "assert_complete_listening_gate", assert_gate)
    monkeypatch.setattr(promoter._validator, "validate_audio_asset_pack", validate)
    monkeypatch.setattr(promoter._validator, "check_attribution", check_attribution)
    return calls


def test_promotes_exact_runtime_pack_with_validated_directory_swap(
    tmp_path: Path,
    _safe_dependencies: dict[str, list[Path]],
) -> None:
    candidate = _candidate(tmp_path)
    target = tmp_path / "installed"
    _write_runtime_pack(target, "old")

    result = promoter.promote_complete_audio_pack(
        candidate.board_manifest,
        CONTENT_DIGEST,
        target=target,
    )

    assert result.changed is True
    assert result.content_digest == CONTENT_DIGEST
    assert _tree_bytes(target) == _tree_bytes(candidate.pack)
    assert len(_safe_dependencies["gate"]) == 2
    assert any("promotion-stage" in path.name for path in _safe_dependencies["validate"])
    assert _safe_dependencies["validate"][-1] == target
    assert not _promotion_artifacts(target)


def test_exact_installed_digest_is_a_noop(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    target = tmp_path / "installed"

    first = promoter.promote_complete_audio_pack(candidate.board_manifest, CONTENT_DIGEST, target=target)

    def fail_if_called(_phase: str) -> None:
        raise AssertionError("a no-op must not enter the swap path")

    second = promoter.promote_complete_audio_pack(
        candidate.board_manifest,
        CONTENT_DIGEST,
        target=target,
        failure_injector=fail_if_called,
    )
    assert first.changed is True
    assert second.changed is False
    assert _tree_bytes(target) == _tree_bytes(candidate.pack)
    assert not _promotion_artifacts(target)


@pytest.mark.parametrize(
    ("status", "current", "message"),
    [
        ("pending", True, "pending"),
        ("rejected", True, "rejected"),
        ("approved", False, "non-current"),
    ],
)
def test_gate_rejection_happens_before_target_mutation(
    tmp_path: Path,
    status: str,
    current: bool,
    message: str,
) -> None:
    candidate = _candidate(tmp_path, status=status, current=current)
    target = tmp_path / "installed"
    _write_runtime_pack(target, "old")
    before = _tree_bytes(target)

    with pytest.raises(ValueError, match=message):
        promoter.promote_complete_audio_pack(candidate.board_manifest, CONTENT_DIGEST, target=target)

    assert _tree_bytes(target) == before
    assert not _promotion_artifacts(target)


def test_wrong_expected_digest_does_not_create_target_state(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    target = tmp_path / "installed"

    with pytest.raises(promoter.PromotionError, match="does not match"):
        promoter.promote_complete_audio_pack(candidate.board_manifest, OTHER_CONTENT_DIGEST, target=target)

    assert not target.exists()
    assert not _promotion_artifacts(target)


@pytest.mark.parametrize("target_name", ["..", ""])
def test_unsafe_final_target_component_is_rejected(tmp_path: Path, target_name: str) -> None:
    candidate = _candidate(tmp_path)
    container = tmp_path / "container"
    container.mkdir()
    target = container / target_name if target_name else Path("/")

    with pytest.raises(promoter.PromotionError, match="Unsafe promotion target"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=target,
        )

    assert not _promotion_artifacts(target)


def test_missing_named_runtime_projection_fails_closed(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    board = candidate.board()
    del board["runtime_projection"]
    candidate.board_manifest.write_text(json.dumps(board), encoding="utf-8")

    with pytest.raises(promoter.PromotionError, match="runtime_projection"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=tmp_path / "installed",
        )


def test_runtime_projection_manifest_hash_is_enforced(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    board = candidate.board()
    board["runtime_projection"]["manifest_sha256"] = "0" * 64
    candidate.board_manifest.write_text(json.dumps(board), encoding="utf-8")

    with pytest.raises(promoter.PromotionError, match="manifest hash"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=tmp_path / "installed",
        )


def test_runtime_manifest_rejects_board_only_receipt(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    runtime = candidate.runtime_manifest()
    runtime["listening_receipt"] = {"status": "approved"}
    candidate.save_runtime_manifest(runtime)

    with pytest.raises(promoter.PromotionError, match="board-only fields"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=tmp_path / "installed",
        )


def test_runtime_inventory_rejects_traversal(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    runtime = candidate.runtime_manifest()
    runtime["inventory"]["files"][0]["path"] = "../ATTRIBUTION.md"
    runtime["inventory"]["digest"] = _raw_inventory_digest(runtime["inventory"]["files"])
    candidate.save_runtime_manifest(runtime)

    with pytest.raises(promoter.PromotionError, match="canonical relative POSIX path"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=tmp_path / "installed",
        )


def test_runtime_inventory_rejects_boolean_schema_version(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    runtime = candidate.runtime_manifest()
    runtime["inventory"]["schema_version"] = True
    candidate.save_runtime_manifest(runtime)

    with pytest.raises(promoter.PromotionError, match="schema version"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=tmp_path / "installed",
        )


def test_candidate_pack_rejects_undeclared_files(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    _write(candidate.pack / "surprise.mp3", b"not declared")

    with pytest.raises(promoter.PromotionError, match=r"extra=.*surprise"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=tmp_path / "installed",
        )


def test_candidate_pack_rejects_symlinks(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    outside = _write(tmp_path / "outside.mp3", candidate.asset.read_bytes())
    candidate.asset.unlink()
    candidate.asset.symlink_to(outside)

    with pytest.raises(promoter.PromotionError, match="symlink"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=tmp_path / "installed",
        )


def test_candidate_pack_rejects_special_files(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    candidate.asset.unlink()
    os.mkfifo(candidate.asset)

    with pytest.raises(promoter.PromotionError, match="special filesystem entry"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=tmp_path / "installed",
        )


def test_symlink_target_is_rejected_without_touching_destination(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    destination = tmp_path / "destination"
    _write_runtime_pack(destination, "operator")
    target = tmp_path / "installed"
    target.symlink_to(destination, target_is_directory=True)
    before = _tree_bytes(destination)

    with pytest.raises(promoter.PromotionError, match="real directory"):
        promoter.promote_complete_audio_pack(candidate.board_manifest, CONTENT_DIGEST, target=target)

    assert target.is_symlink()
    assert _tree_bytes(destination) == before
    assert not promoter._lock_path(target).exists()


def test_runtime_inventory_requires_every_retained_master(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    runtime = candidate.runtime_manifest()
    runtime["inventory"]["files"] = [
        item for item in runtime["inventory"]["files"] if item["path"] != "provenance/source-masters/station.mp3"
    ]
    runtime["inventory"]["digest"] = _raw_inventory_digest(runtime["inventory"]["files"])
    candidate.save_runtime_manifest(runtime)

    with pytest.raises(promoter.PromotionError, match="controls/assets/masters"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=tmp_path / "installed",
        )


@pytest.mark.parametrize("mutation", ["extra", "changed"])
def test_dirty_or_unexpected_existing_target_is_rejected(tmp_path: Path, mutation: str) -> None:
    candidate = _candidate(tmp_path)
    target = tmp_path / "installed"
    _write_runtime_pack(target, "old")
    if mutation == "extra":
        _write(target / "operator-note.txt", b"do not discard")
    else:
        (target / "audio" / "station.mp3").write_bytes(b"locally edited")
    before = _tree_bytes(target)

    with pytest.raises(promoter.PromotionError, match=r"dirty|differs"):
        promoter.promote_complete_audio_pack(candidate.board_manifest, CONTENT_DIGEST, target=target)

    assert _tree_bytes(target) == before
    assert not _promotion_artifacts(target)


def test_lock_contention_rejects_without_touching_lock_or_target(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    target = tmp_path / "installed"
    _write_runtime_pack(target, "old")
    before = _tree_bytes(target)
    lock = promoter._lock_path(target)
    lock.write_text("another promoter\n", encoding="utf-8")

    with pytest.raises(promoter.PromotionError, match="holds the lock"):
        promoter.promote_complete_audio_pack(candidate.board_manifest, CONTENT_DIGEST, target=target)

    assert lock.read_text(encoding="utf-8") == "another promoter\n"
    assert _tree_bytes(target) == before


def test_staging_validation_failure_leaves_existing_target_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(tmp_path)
    target = tmp_path / "installed"
    _write_runtime_pack(target, "old")
    before = _tree_bytes(target)
    original_validate = promoter._validator.validate_audio_asset_pack

    def reject_stage(pack_dir: Path) -> SimpleNamespace:
        if "promotion-stage" in Path(pack_dir).name:
            raise ValueError("injected stage validation failure")
        return original_validate(pack_dir)

    monkeypatch.setattr(promoter._validator, "validate_audio_asset_pack", reject_stage)

    with pytest.raises(promoter.PromotionError, match="injected stage validation failure"):
        promoter.promote_complete_audio_pack(candidate.board_manifest, CONTENT_DIGEST, target=target)

    assert _tree_bytes(target) == before
    assert not _promotion_artifacts(target)


def test_stage_cleanup_failure_still_releases_promotion_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(tmp_path)
    target = tmp_path / "installed"

    def fail_copy(_projection: object, _stage: Path) -> None:
        raise RuntimeError("injected copy failure")

    original_rmtree = promoter.shutil.rmtree

    def fail_stage_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if "promotion-stage" in Path(path).name:
            raise OSError("injected cleanup failure")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(promoter, "_copy_projection", fail_copy)
    monkeypatch.setattr(promoter.shutil, "rmtree", fail_stage_cleanup)

    with pytest.raises(OSError, match="injected cleanup failure"):
        promoter.promote_complete_audio_pack(candidate.board_manifest, CONTENT_DIGEST, target=target)

    assert not promoter._lock_path(target).exists()


@pytest.mark.parametrize("phase", ["after-backup", "after-swap", "after-validation"])
def test_injected_swap_failure_restores_target_byte_identically(tmp_path: Path, phase: str) -> None:
    candidate = _candidate(tmp_path)
    target = tmp_path / "installed"
    _write_runtime_pack(target, "old")
    before = _tree_bytes(target)

    def inject(current_phase: str) -> None:
        if current_phase == phase:
            raise RuntimeError(f"injected {phase}")

    with pytest.raises(RuntimeError, match=f"injected {phase}"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=target,
            failure_injector=inject,
        )

    assert _tree_bytes(target) == before
    assert not _promotion_artifacts(target)


def test_injected_failure_restores_absent_target(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    target = tmp_path / "installed"

    def inject(phase: str) -> None:
        if phase == "after-swap":
            raise RuntimeError("injected absent-target rollback")

    with pytest.raises(RuntimeError, match="absent-target"):
        promoter.promote_complete_audio_pack(
            candidate.board_manifest,
            CONTENT_DIGEST,
            target=target,
            failure_injector=inject,
        )

    assert not target.exists()
    assert not _promotion_artifacts(target)


def test_cli_requires_explicit_manifest_and_digest() -> None:
    with pytest.raises(SystemExit):
        promoter._parse_args([])

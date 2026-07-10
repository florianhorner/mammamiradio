"""Unit tests for the shared filesystem containment guard.

Callers across restart_handoff.py and downloader.py delegate to this helper
for symlink/path-containment decisions; these tests lock down its contract
directly instead of relying on caller behavior to keep exercising it.
"""

from pathlib import Path

from mammamiradio.core.path_safety import safe_path_within


def test_path_inside_root_is_returned_resolved(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "file.mp3"
    target.write_bytes(b"x")

    assert safe_path_within(target, root) == target.resolve()


def test_path_outside_root_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"x")

    assert safe_path_within(outside, root) is None


def test_non_existent_path_still_containment_checked(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    assert safe_path_within(root / "missing.mp3", root) == (root / "missing.mp3").resolve()
    assert safe_path_within(tmp_path / "outside" / "missing.mp3", root) is None


def test_symlink_target_inside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    real = root / "real.mp3"
    real.write_bytes(b"x")
    link = root / "link.mp3"
    link.symlink_to(real)

    assert safe_path_within(link, root, reject_symlinks=True) is None
    assert safe_path_within(link, root, reject_symlinks=False) == real.resolve()


def test_symlink_target_outside_root_always_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"x")
    link = root / "link.mp3"
    link.symlink_to(outside)

    assert safe_path_within(link, root, reject_symlinks=False) is None
    assert safe_path_within(link, root, reject_symlinks=True) is None


def test_root_itself_a_symlink_is_not_rejected_by_reject_symlinks_on_the_leaf(tmp_path):
    # reject_symlinks only inspects the leaf `path` argument, not `root`. A
    # symlinked root resolves "contained" relative to itself, so callers that
    # pass an untrusted root must validate the root separately (see
    # restart_handoff._safe_handoff_dir / downloader.prune_stale_tmp_files's
    # own tmp_dir.is_symlink() guard) rather than relying on this call alone.
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    target = real_root / "file.mp3"
    target.write_bytes(b"x")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    resolved = safe_path_within(linked_root / "file.mp3", linked_root, reject_symlinks=True)
    assert resolved == target.resolve()


def test_resolve_failure_returns_none_without_raising(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "file.mp3"
    target.write_bytes(b"x")

    def boom(self, strict=False):
        raise RuntimeError("symlink loop")

    monkeypatch.setattr(Path, "resolve", boom)
    assert safe_path_within(target, root) is None

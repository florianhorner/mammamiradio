"""Unit tests for the shared filesystem containment guard.

Callers across restart_handoff.py and downloader.py delegate to this helper
for symlink/path-containment decisions; these tests lock down its contract
directly instead of relying on caller behavior to keep exercising it.
"""

from pathlib import Path
from unittest.mock import MagicMock

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


def test_real_symlink_cycle_is_refused_even_when_resolve_does_not_raise(tmp_path):
    """A cycle must be refused on its own merits, not via a resolve() exception.

    The sibling test above monkeypatches ``Path.resolve`` to raise, which pins
    the handling of an exception rather than the handling of a cycle. Python
    3.12 and earlier raised ``RuntimeError`` here, so the two looked identical;
    3.13 and later return the unresolved path and raise nothing, and only a real cycle
    tells the two apart.
    """

    root = tmp_path / "root"
    root.mkdir()
    loop = root / "loop.mp3"
    loop.symlink_to(loop.name)

    try:
        loop.resolve(strict=False)
        resolve_raised = False
    except (OSError, RuntimeError):
        resolve_raised = True

    assert safe_path_within(loop, root) is None, (
        f"symlink cycle must be refused whether or not resolve() raised (resolve raised: {resolve_raised})"
    )
    assert safe_path_within(loop, root, reject_symlinks=True) is None


def test_cycle_in_a_parent_component_is_refused_for_reject_symlinks_callers(tmp_path):
    """reject_symlinks=True does not make the cycle probe redundant.

    is_symlink() calls lstat() and pathlib swallows ELOOP, so it answers False
    when the cycle is in a parent component rather than the leaf. Nine call
    sites pass reject_symlinks=True; without this case a future reader could
    conclude the probe is skippable for them and reopen the hole for every
    path under a looped directory.
    """

    root = tmp_path / "root"
    root.mkdir()
    (root / "a").symlink_to("b")
    (root / "b").symlink_to("a")
    child = root / "a" / "child.mp3"

    assert child.is_symlink() is False, "precondition: the leaf itself is not a symlink"
    assert safe_path_within(child, root, reject_symlinks=True) is None
    assert safe_path_within(child, root) is None


def test_missing_file_is_not_treated_as_a_containment_failure(tmp_path):
    """Only a cycle is an escape. A missing path is the caller's business.

    Cleanup and admission callers hand in paths that legitimately may not
    exist; refusing them here would turn an ordinary absent candidate into a
    containment error and change behaviour far beyond the cycle fix.
    """

    root = tmp_path / "root"
    root.mkdir()
    absent = root / "not-created-yet.mp3"

    assert safe_path_within(absent, root) == absent.resolve()

    dangling = root / "dangling.mp3"
    dangling.symlink_to("missing-target.mp3")
    assert safe_path_within(dangling, root) == (root / "missing-target.mp3").resolve()


def test_path_like_whose_resolve_returns_a_non_path_is_refused(tmp_path):
    """A resolution result that is not a Path is not containment.

    Cleanup callers are exercised with ``MagicMock(spec=Path)`` doubles. A
    spec'd mock passes ``isinstance(path, Path)`` but answers ``resolve()``
    with a plain mock, and that mock answers ``is_relative_to()`` truthily.
    Without this guard the helper reports containment for an object that was
    never a path, and callers that protect contained files stop acting.
    """

    root = tmp_path / "root"
    root.mkdir()
    double = MagicMock(spec=Path)

    assert isinstance(double, Path), "precondition: a spec'd mock passes isinstance"
    assert safe_path_within(double, root) is None


def test_producer_ownership_helpers_refuse_a_cycle_without_raising(tmp_path):
    """Producer ownership checks must answer, never raise, on malformed state.

    `_is_tmp_render` and `_is_under` decide whether the producer owns a file
    and may delete it. They previously caught only OSError, so the
    RuntimeError that Python 3.12 and earlier raised on a symlink cycle
    escaped into the audio path instead of returning a verdict. An exception
    there risks the stream; a False does not.
    """

    from mammamiradio.scheduling.producer import _is_under

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    loop = tmp_dir / "loop.mp3"
    loop.symlink_to(loop.name)

    assert _is_under(loop, tmp_dir) is False
    assert _is_under(tmp_dir / "plain.mp3", tmp_dir) is True
    assert _is_under(tmp_path / "elsewhere.mp3", tmp_dir) is False


def test_malformed_path_degrades_to_none_instead_of_raising(tmp_path):
    """Malformed input is a skipped candidate, never an exception.

    Callers run this during cleanup and startup admission, where an escaping
    exception is worse than a refused path. A null byte makes resolve() raise
    ValueError, which sits outside the OSError/RuntimeError family the rest of
    the function handles.
    """

    root = tmp_path / "root"
    root.mkdir()

    assert safe_path_within(Path("/tmp/a\x00b"), root) is None
    assert safe_path_within(root / "ok.mp3", Path("/tmp/a\x00b")) is None

"""Canonical, hardware-neutral identity for tracked release content."""

import hashlib
import os
import stat
import struct
import subprocess
import tempfile
from pathlib import Path

_CONTENT_DOMAIN = b"mammamiradio/release-content\0\x01"
_SUPPORTED_BLOB_MODES = frozenset({b"100644", b"100755", b"120000"})


def git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1", LC_ALL="C")
    return env


def git(*args: str, cwd: Path):
    return subprocess.run(["git", "--no-replace-objects", *args], cwd=cwd, env=git_env(), capture_output=True)


def resolve_commit(repo_root: Path, ref: str) -> str:
    result = git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", cwd=repo_root)
    value = result.stdout.strip().decode("ascii", errors="replace").casefold()
    if result.returncode != 0 or len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"cannot resolve release commit {ref!r}")
    return value


def partitioned_entries(repo_root: Path, commit: str, *, exclude_entry):
    resolved = resolve_commit(repo_root, commit)
    tree = git("ls-tree", "-r", "-z", "--full-tree", resolved, cwd=repo_root)
    if tree.returncode != 0:
        detail = tree.stderr.decode("utf-8", errors="replace").strip() or "git ls-tree failed"
        raise ValueError(f"cannot enumerate release content: {detail}")
    if tree.stdout and not tree.stdout.endswith(b"\0"):
        raise ValueError("git ls-tree returned a non-NUL-terminated record")
    included, excluded = [], []
    seen: set[bytes] = set()
    for record in filter(None, tree.stdout.split(b"\0")):
        try:
            header, path = record.split(b"\t", 1)
            mode, kind, oid = header.split(b" ", 2)
        except ValueError as exc:
            raise ValueError("git ls-tree returned a malformed record") from exc
        if not path or path in seen:
            raise ValueError("git ls-tree returned an empty or duplicate tracked path")
        seen.add(path)
        if kind != b"blob" or mode not in _SUPPORTED_BLOB_MODES:
            raise ValueError(f"unsupported tracked entry at {os.fsdecode(path)!r}: {mode!r} {kind!r}")
        entry = (path, mode, oid)
        (excluded if exclude_entry(entry) else included).append(entry)
    included.sort(key=lambda entry: entry[0])
    excluded.sort(key=lambda entry: entry[0])
    return resolved, included, excluded


def _digest(entries):
    return hashlib.sha256(_CONTENT_DOMAIN + struct.pack(">Q", len(entries)))


def _update_header(digest, path: bytes, mode: bytes, size: int) -> None:
    digest.update(struct.pack(">Q", len(path)) + path + struct.pack(">I", int(mode, 8)) + struct.pack(">Q", size))


def tracked_content_sha256(repo_root: Path, commit: str, *, exclude_entry):
    resolved, entries, _excluded = partitioned_entries(repo_root, commit, exclude_entry=exclude_entry)
    digest = _digest(entries)
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            ["git", "--no-replace-objects", "cat-file", "--batch"],
            cwd=repo_root,
            env=git_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        if process.stdin is None or process.stdout is None:
            process.kill()
            process.wait()
            raise ValueError("cannot open git cat-file pipes")
        try:
            for path, mode, oid in entries:
                process.stdin.write(oid + b"\n")
                process.stdin.flush()
                header = process.stdout.readline(512)
                if not header.endswith(b"\n"):
                    raise ValueError("git cat-file returned a malformed blob header")
                fields = header[:-1].split(b" ")
                if len(fields) != 3 or fields[0] != oid or fields[1] != b"blob":
                    raise ValueError("git cat-file returned an unexpected object")
                try:
                    blob_size = int(fields[2])
                except ValueError as exc:
                    raise ValueError("git cat-file returned an invalid blob size") from exc
                if not 0 <= blob_size < 2**64:
                    raise ValueError("git cat-file returned an out-of-range blob size")
                _update_header(digest, path, mode, blob_size)
                remaining = blob_size
                while remaining:
                    chunk = process.stdout.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("git cat-file truncated a blob")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if process.stdout.read(1) != b"\n":
                    raise ValueError("git cat-file returned a malformed blob terminator")
            process.stdin.close()
            status = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if status != 0:
            stderr.seek(0)
            detail = stderr.read().decode("utf-8", errors="replace").strip() or "git cat-file failed"
            raise ValueError(f"cannot read release blobs: {detail}")
        if process.stdout.read(1):
            raise ValueError("git cat-file returned unexpected trailing output")
    return resolved, digest.hexdigest()


def _worktree_blob(repo_root: Path, path: bytes, mode: bytes) -> bytes:
    full_path = os.path.join(os.fsencode(repo_root.resolve()), path)
    try:
        metadata = os.lstat(full_path)
        if mode == b"120000":
            if not stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"tracked symlink {os.fsdecode(path)!r} is not checked out as a symlink")
            return os.readlink(full_path)
        executable = bool(metadata.st_mode & 0o111)
        if not stat.S_ISREG(metadata.st_mode) or executable != (mode == b"100755"):
            raise ValueError(f"tracked file mode differs from HEAD at {os.fsdecode(path)!r}")
        with open(full_path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ValueError(f"cannot read tracked worktree path {os.fsdecode(path)!r}: {exc}") from exc


def worktree_content_sha256(repo_root: Path, commit: str, *, exclude_entry):
    resolved, entries, excluded = partitioned_entries(repo_root, commit, exclude_entry=exclude_entry)
    digest = _digest(entries)
    for path, mode, _oid in entries:
        blob = _worktree_blob(repo_root, path, mode)
        _update_header(digest, path, mode, len(blob))
        digest.update(blob)
    for path, mode, oid in excluded:
        blob = _worktree_blob(repo_root, path, mode)
        committed = git("cat-file", "blob", oid.decode("ascii"), cwd=repo_root)
        if committed.returncode != 0 or committed.stdout != blob:
            raise ValueError(f"excluded tracked path differs from HEAD at {os.fsdecode(path)!r}")
    return resolved, digest.hexdigest()

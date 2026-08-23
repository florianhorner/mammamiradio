"""Byte-preserving Git access used by the landing policy tools."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from .errors import GitError

_HEX_RE = re.compile(r"^[0-9a-f]+$")


@dataclass(frozen=True)
class TreeEntry:
    """One raw record from ``git ls-tree -r -z --full-tree``."""

    raw: bytes
    mode: bytes
    kind: bytes
    oid: str
    path: bytes

    @property
    def raw_with_nul(self) -> bytes:
        return self.raw + b"\0"


class GitRepository:
    """Small injectable boundary around Git's byte-oriented plumbing commands."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        object_format = self.run(("rev-parse", "--show-object-format")).strip()
        if object_format == b"sha1":
            self.oid_length = 40
        elif object_format == b"sha256":
            self.oid_length = 64
        else:
            rendered = object_format.decode("ascii", errors="replace")
            raise GitError(f"unsupported Git object format {rendered!r}")

    @classmethod
    def discover(cls, cwd: Path | None = None) -> GitRepository:
        result = cls._invoke(("rev-parse", "--show-toplevel"), cwd=cwd)
        if result.returncode != 0:
            raise GitError("not a Git repository (git rev-parse --show-toplevel failed)")
        raw_root = result.stdout.rstrip(b"\r\n")
        if not raw_root:
            raise GitError("Git returned an empty repository root")
        return cls(Path(os.fsdecode(raw_root)))

    @staticmethod
    def _invoke(
        args: Iterable[str],
        *,
        cwd: Path | None,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        return subprocess.run(
            ["git", "--no-replace-objects", *args],
            cwd=cwd,
            env=env,
            input=input_bytes,
            capture_output=True,
            check=False,
        )

    def run(
        self,
        args: Iterable[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> bytes:
        args_tuple = tuple(args)
        result = self._invoke(args_tuple, cwd=self.root, input_bytes=input_bytes)
        if check and result.returncode != 0:
            command = "git " + " ".join(args_tuple)
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise GitError(f"{command} failed with exit {result.returncode}{suffix}")
        return result.stdout

    def run_result(
        self,
        args: Iterable[str],
        *,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._invoke(tuple(args), cwd=self.root, input_bytes=input_bytes)

    def resolve_commit(self, ref: str) -> str:
        if not ref:
            raise GitError("empty commit reference")
        result = self.run_result(("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"))
        if result.returncode != 0:
            raise GitError(f"commit {ref!r} does not resolve uniquely in this repository")
        lines = result.stdout.splitlines()
        if len(lines) != 1:
            raise GitError(f"commit {ref!r} did not resolve to exactly one object ID")
        try:
            oid = lines[0].decode("ascii").lower()
        except UnicodeDecodeError as exc:
            raise GitError(f"commit {ref!r} resolved to a non-ASCII object ID") from exc
        if len(oid) != self.oid_length or _HEX_RE.fullmatch(oid) is None:
            raise GitError(f"commit {ref!r} resolved to malformed object ID {oid!r}")
        return oid

    def resolve_abbreviated_commit(self, value: object) -> str:
        if not isinstance(value, str):
            raise GitError("ledger commit is not a string")
        if not 7 <= len(value) <= self.oid_length or re.fullmatch(r"[0-9a-fA-F]+", value) is None:
            raise GitError(f"ledger commit {value!r} is not a hexadecimal object ID")
        resolved = self.resolve_commit(value)
        if not resolved.startswith(value.lower()):
            raise GitError(f"ledger commit {value!r} resolves to {resolved}, which does not have that object-ID prefix")
        return resolved

    def resolve_full_commit(self, value: str) -> str:
        if len(value) != self.oid_length or _HEX_RE.fullmatch(value) is None:
            raise GitError(f"full commit {value!r} is not a lowercase object ID")
        resolved = self.resolve_commit(value)
        if resolved != value:
            raise GitError(f"full commit {value!r} resolves to different object {resolved}")
        return resolved

    def head(self) -> str:
        return self.resolve_commit("HEAD")

    def status_bytes(self) -> bytes:
        return self.run(("status", "--porcelain=v2", "--untracked-files=all", "-z"))

    def tree_entries(self, commit: str) -> tuple[TreeEntry, ...]:
        resolved = self.resolve_commit(commit)
        output = self.run(("ls-tree", "-r", "-z", "--full-tree", resolved))
        if not output:
            return ()
        if not output.endswith(b"\0"):
            raise GitError("git ls-tree returned a non-NUL-terminated record stream")

        entries: list[TreeEntry] = []
        for raw in output[:-1].split(b"\0"):
            metadata, separator, path = raw.partition(b"\t")
            if not separator:
                raise GitError("git ls-tree returned a record without a path separator")
            fields = metadata.split(b" ")
            if len(fields) != 3:
                raise GitError("git ls-tree returned malformed entry metadata")
            mode, kind, raw_oid = fields
            try:
                oid = raw_oid.decode("ascii").lower()
            except UnicodeDecodeError as exc:
                raise GitError("git ls-tree returned a non-ASCII object ID") from exc
            if len(oid) != self.oid_length or _HEX_RE.fullmatch(oid) is None:
                raise GitError(f"git ls-tree returned malformed object ID {oid!r}")
            if not path:
                raise GitError("git ls-tree returned an empty path")
            entries.append(TreeEntry(raw=raw, mode=mode, kind=kind, oid=oid, path=path))
        return tuple(entries)

    def count_added_paths(
        self,
        base: str,
        target: str,
        *,
        pathspec: str,
        max_paths: int,
        max_path_bytes: int,
    ) -> int:
        """Count added paths up to ``max_paths + 1`` without buffering the diff."""

        if max_paths < 0 or max_path_bytes < 1:
            raise GitError("added-path limits must be non-negative and non-zero")
        base_oid = self.resolve_commit(base)
        target_oid = self.resolve_commit(target)
        env = os.environ.copy()
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        command = [
            "git",
            "--no-replace-objects",
            "diff",
            "--no-renames",
            "--diff-filter=A",
            "--name-only",
            "-z",
            base_oid,
            target_oid,
            "--",
            pathspec,
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise GitError(f"cannot start git diff for added-path preflight: {exc}") from exc

        stdout = cast(BinaryIO, process.stdout)
        stderr = cast(BinaryIO, process.stderr)
        count = 0
        pending = b""
        try:
            while chunk := stdout.read(64 * 1024):
                pending += chunk
                records = pending.split(b"\0")
                pending = records.pop()
                if any(not record for record in records):
                    raise GitError("git diff returned an empty added path")
                if any(len(record) > max_path_bytes for record in records):
                    raise GitError(f"git diff returned a path longer than {max_path_bytes} bytes")
                count += len(records)
                if len(pending) > max_path_bytes:
                    raise GitError(f"git diff returned a path longer than {max_path_bytes} bytes")
                if count > max_paths:
                    process.kill()
                    process.wait()
                    return count

            detail = stderr.read().decode("utf-8", errors="replace").strip()
            returncode = process.wait()
            if returncode != 0:
                suffix = f": {detail}" if detail else ""
                raise GitError(f"git diff added-path preflight failed with exit {returncode}{suffix}")
            if pending:
                raise GitError("git diff returned a non-NUL-terminated path stream")
            return count
        except OSError as exc:
            raise GitError(f"cannot read git diff added-path preflight: {exc}") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            stdout.close()
            stderr.close()

    def read_blobs(
        self,
        object_ids: Iterable[str],
        *,
        max_bytes: int,
        max_total_bytes: int,
    ) -> dict[str, bytes]:
        unique_ids = tuple(dict.fromkeys(object_ids))
        if not unique_ids:
            return {}
        for oid in unique_ids:
            if len(oid) != self.oid_length or _HEX_RE.fullmatch(oid) is None:
                raise GitError(f"refusing malformed blob object ID {oid!r}")

        payload = b"".join(oid.encode("ascii") + b"\n" for oid in unique_ids)
        check_output = self.run(("cat-file", "--batch-check"), input_bytes=payload)
        check_lines = check_output.splitlines()
        if len(check_lines) != len(unique_ids):
            raise GitError("git cat-file --batch-check returned the wrong number of records")

        total_size = 0
        for requested_oid, header in zip(unique_ids, check_lines, strict=True):
            fields = header.split(b" ")
            if len(fields) == 2 and fields[1] == b"missing":
                raise GitError(f"tree references missing object {requested_oid}")
            if len(fields) != 3:
                raise GitError("git cat-file --batch-check returned malformed metadata")
            raw_actual_oid, kind, raw_size = fields
            try:
                actual_oid = raw_actual_oid.decode("ascii").lower()
                size = int(raw_size)
            except (UnicodeDecodeError, ValueError) as exc:
                raise GitError("git cat-file --batch-check returned malformed object metadata") from exc
            if actual_oid != requested_oid or kind != b"blob":
                rendered_kind = kind.decode("ascii", errors="replace")
                raise GitError(
                    f"object {requested_oid} resolved as {actual_oid} ({rendered_kind}), not the requested blob"
                )
            if size < 0 or size > max_bytes:
                raise GitError(f"receipt blob {requested_oid} is {size} bytes; maximum is {max_bytes}")
            total_size += size
            if total_size > max_total_bytes:
                raise GitError(
                    f"receipt blobs total more than {max_total_bytes} bytes; reserved namespace is unbounded"
                )

        output = self.run(("cat-file", "--batch"), input_bytes=payload)
        position = 0
        blobs: dict[str, bytes] = {}

        for requested_oid in unique_ids:
            header_end = output.find(b"\n", position)
            if header_end < 0:
                raise GitError("git cat-file returned a truncated header")
            header = output[position:header_end]
            position = header_end + 1
            fields = header.split(b" ")
            if len(fields) == 2 and fields[1] == b"missing":
                raise GitError(f"tree references missing object {requested_oid}")
            if len(fields) != 3:
                raise GitError("git cat-file returned malformed batch metadata")
            raw_actual_oid, kind, raw_size = fields
            try:
                actual_oid = raw_actual_oid.decode("ascii").lower()
                size = int(raw_size)
            except (UnicodeDecodeError, ValueError) as exc:
                raise GitError("git cat-file returned malformed object metadata") from exc
            if actual_oid != requested_oid or kind != b"blob":
                rendered_kind = kind.decode("ascii", errors="replace")
                raise GitError(
                    f"object {requested_oid} resolved as {actual_oid} ({rendered_kind}), not the requested blob"
                )
            if size < 0 or size > max_bytes:
                raise GitError(f"receipt blob {requested_oid} is {size} bytes; maximum is {max_bytes}")
            end = position + size
            if end >= len(output) or output[end : end + 1] != b"\n":
                raise GitError(f"git cat-file returned truncated bytes for blob {requested_oid}")
            blobs[requested_oid] = output[position:end]
            position = end + 1

        if position != len(output):
            raise GitError("git cat-file returned unexpected trailing output")
        return blobs

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        ancestor_oid = self.resolve_commit(ancestor)
        descendant_oid = self.resolve_commit(descendant)
        result = self.run_result(("merge-base", "--is-ancestor", ancestor_oid, descendant_oid))
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise GitError(f"git merge-base --is-ancestor failed with exit {result.returncode}{suffix}")

    def origin_url(self) -> str:
        output = self.run(("remote", "get-url", "origin")).rstrip(b"\r\n")
        if not output:
            raise GitError("origin remote has no URL")
        try:
            return output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitError("origin remote URL is not valid UTF-8") from exc

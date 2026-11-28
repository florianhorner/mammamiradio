#!/usr/bin/env python3
"""Check that every pinned audio-pack generator revision has its provenance tag.

``mammamiradio/assets/imaging/manifest.json`` pins project-authored sources to
``generator.revision``. A squash merge leaves that commit reachable from no
branch, so a tag ``provenance/audio-pack-<first 8 hex>`` keeps it fetchable
(see docs/release-process.md "Provenance tags").

On a re-pin pull request the new commit is still reachable from the PR branch,
so the blob check in ``validate_audio_asset_pack.py`` passes even when nobody
pushed the tag; ``main`` would only fail after the merge. This check closes
that gap: it asks the remote (never the local clone, whose tags a contributor
controls) for each tag and requires it to peel to exactly the pinned commit.

Usage:
    python scripts/check_provenance_tags.py
    python scripts/check_provenance_tags.py --remote https://github.com/OWNER/REPO.git
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

if __package__ in (None, ""):  # run as `python scripts/check_provenance_tags.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_audio_asset_pack import (
    DEFAULT_PACK_DIR,
    GIT_REVISION_RE,
    MANIFEST_NAME,
    PROVENANCE_TAG_PREFIX,
    REPO_ROOT,
    provenance_tag_for,
)

DEFAULT_MANIFEST = DEFAULT_PACK_DIR / MANIFEST_NAME
LS_REMOTE_ATTEMPTS = 3
LS_REMOTE_TIMEOUT_SEC = 30

# Runs ``git ls-remote`` and returns its stdout; raises RuntimeError on failure.
LsRemote = Callable[[str, str], str]


class ProvenanceTagError(RuntimeError):
    """Raised when the remote tags cannot be listed at all."""


def pinned_revisions(manifest: Mapping[str, object]) -> list[str]:
    """Return each distinct, well-formed ``generator.revision`` in manifest order.

    Malformed revisions are left to ``validate_audio_asset_pack.py``, which
    reports them with full context; this check only needs the tag names.
    """
    revisions: list[str] = []
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        return revisions
    for source in sources:
        if not isinstance(source, dict):
            continue
        generator = source.get("generator")
        if not isinstance(generator, dict):
            continue
        revision = generator.get("revision")
        if isinstance(revision, str) and GIT_REVISION_RE.fullmatch(revision) and revision not in revisions:
            revisions.append(revision)
    return revisions


def _run_ls_remote(remote: str, pattern: str) -> str:
    last_detail = ""
    for attempt in range(1, LS_REMOTE_ATTEMPTS + 1):
        try:
            completed = subprocess.run(
                ["git", "ls-remote", "--tags", remote, pattern],
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                timeout=LS_REMOTE_TIMEOUT_SEC,
                env={**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"},
            )
        except FileNotFoundError as exc:
            raise ProvenanceTagError("git is required to check provenance tags") from exc
        except subprocess.TimeoutExpired:
            last_detail = f"git ls-remote timed out after {LS_REMOTE_TIMEOUT_SEC}s"
        else:
            if completed.returncode == 0:
                return completed.stdout.decode("utf-8", errors="replace")
            last_detail = " ".join(completed.stderr.decode("utf-8", errors="replace").split())
        if attempt < LS_REMOTE_ATTEMPTS:
            time.sleep(2 * attempt)
    raise ProvenanceTagError(
        f"could not list tags on {remote!r} ({last_detail or 'no detail from git'}); "
        "check that the remote is reachable and run the check again"
    )


def parse_ls_remote(output: str) -> dict[str, str]:
    """Map each tag ref to the commit it finally points at.

    ``git ls-remote`` prints an annotated tag twice: the tag object under
    ``refs/tags/X`` and the peeled target under ``refs/tags/X^{}``. A
    lightweight tag appears once, already pointing at its commit.
    """
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        if ref.endswith("^{}"):
            peeled[ref[: -len("^{}")]] = sha
        else:
            direct[ref] = sha
    return {ref: peeled.get(ref, sha) for ref, sha in direct.items()}


def check_provenance_tags(
    revisions: Iterable[str],
    *,
    remote: str,
    ls_remote: LsRemote = _run_ls_remote,
) -> list[str]:
    """Return one plain-English error per pinned revision whose tag is wrong."""
    revisions = list(revisions)
    if not revisions:
        return []
    tags = parse_ls_remote(ls_remote(remote, f"refs/tags/{PROVENANCE_TAG_PREFIX}*"))
    errors: list[str] = []
    for revision in revisions:
        tag = provenance_tag_for(revision)
        target = tags.get(f"refs/tags/{tag}")
        push_fix = (
            f"`git tag -a {tag} {revision} -m 'Keep audio-pack generator revision fetchable'` "
            f"then `git push origin refs/tags/{tag}`"
        )
        if target is None:
            errors.append(
                f"the manifest pins generator revision {revision}, but the tag {tag!r} is not on {remote}. "
                f"Without it the commit becomes unfetchable once the branch is squash-merged. "
                f"Push the tag from a clone that has the commit: {push_fix}"
            )
        elif target != revision:
            errors.append(
                f"the tag {tag!r} on {remote} points at {target}, not the pinned generator revision {revision}. "
                f"Provenance tags are never moved: if the manifest is right, re-pin to a revision whose "
                f"8-character prefix has no tag yet; if the manifest is wrong, fix generator.revision to {target}"
            )
    return errors


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Imaging manifest to read")
    parser.add_argument(
        "--remote",
        default="origin",
        help="Remote name or URL whose tags count (CI passes the base repository, never a fork)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read the imaging manifest {args.manifest}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(manifest, dict):
        print(f"ERROR: the imaging manifest {args.manifest} is not a JSON object", file=sys.stderr)
        return 2
    revisions = pinned_revisions(manifest)
    try:
        errors = check_provenance_tags(revisions, remote=args.remote)
    except ProvenanceTagError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("ERROR: audio-pack provenance tags are missing or wrong:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Provenance tags OK: {len(revisions)} pinned revision(s) tagged on {args.remote}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

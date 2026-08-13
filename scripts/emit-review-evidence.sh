#!/usr/bin/env bash
# emit-review-evidence.sh — write proof/preship-review.json from the local gstack ledger.
#
# Runtime-independent half of the pre-ship evidence gate. The Claude PreToolUse hook
# (scripts/hooks/require-preship-squad.sh) cannot fire in Codex — ~/.codex/config.toml has
# no hook layer at all — so CI needs evidence that travels WITH the PR. This script finds
# the newest review/adversarial-review ledger entry FOR THIS REPO whose commit is HEAD or
# an ancestor of HEAD and pins it into a committed, fixed-name artifact.
#
# Fixed name on purpose: naming the file after HEAD cannot work, because committing the
# file changes HEAD (and rebase/amend/squash move it again). The reviewed commit is an
# internal field instead, and the CI checker walks ancestry. Fixed-name + overwrite also
# matches the existing proof/ scratch convention (proof/checks.txt, review-findings.json)
# rather than the append-only receipt convention (see CONTRIBUTING.md).
#
# Two deliberate hardenings, both from adversarial review:
#   - The ledger scan is SCOPED to this repo's project dirs. An unscoped scan across all
#     of ~/.gstack/projects let another project's review satisfy this repo's gate via a
#     7-char SHA prefix collision (~0.9% odds today, growing with history).
#   - Ledger commits are passed to git UNTRUNCATED and the artifact stores the fully
#     resolved 40-char SHA, so prefix ambiguity can neither forge nor break evidence.
#
# The ledger read is INLINED rather than shelled out to gstack-review-read: that binary
# lives in the gstack-upgrade clobber zone, and a product repo must not hard-depend on an
# unversioned third-party binary (see retro/docs/gate-design-comparison.md).
#
# Usage: scripts/emit-review-evidence.sh          (run after the pre-ship review squad)
# Honors $GSTACK_HOME (default ~/.gstack). Exits 1 with problem/cause/fix if no
# qualifying entry exists.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

python3 - <<'PY'
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

ALLOWED_SKILLS = {"review", "adversarial-review"}
COMMIT_SENTINELS = {"uncommitted", "unknown", "pending"}
LEDGER_ROOT = os.path.join(os.environ.get("GSTACK_HOME", os.path.expanduser("~/.gstack")), "projects")
OUT = os.path.join("proof", "preship-review.json")


def die(problem, cause, fix):
    print(f"emit-review-evidence: {problem}\n  cause: {cause}\n  fix:   {fix}", file=sys.stderr)
    raise SystemExit(1)


def git(*args):
    res = subprocess.run(["git", *args], capture_output=True, text=True)
    return res.returncode, res.stdout.strip()


def parse_ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


rc, head = git("rev-parse", "HEAD")
if rc != 0:
    die("not a git repository", "git rev-parse HEAD failed", "run from inside the repo")

# Repo identity for ledger scoping: the remote URL's basename (else the toplevel dirname).
# A qualifying project dir is exactly `<repo>` or `*-<repo>` (gstack slugs are
# `<owner>-<repo>`). The dash-anchored suffix match prevents e.g. `radio` matching
# `fakeitaliradio`.
rc, url = git("remote", "get-url", "origin")
if rc == 0 and url:
    repo_name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1])
else:
    repo_name = os.path.basename(os.getcwd())

if not os.path.isdir(LEDGER_ROOT):
    die(
        "no gstack ledger found",
        f"{LEDGER_ROOT} does not exist on this machine",
        "run the pre-ship review squad (/review via /ship) first; it writes the ledger",
    )

project_dirs = [
    os.path.join(LEDGER_ROOT, name)
    for name in os.listdir(LEDGER_ROOT)
    if os.path.isdir(os.path.join(LEDGER_ROOT, name))
    and (name == repo_name or name.endswith(f"-{repo_name}"))
]
if not project_dirs:
    die(
        f"no ledger directory for repo '{repo_name}'",
        f"nothing under {LEDGER_ROOT} is named '{repo_name}' or '*-{repo_name}'",
        "run the pre-ship review squad in this repo first; it creates the project dir",
    )

# Line-based read is fine here: entries written per the ledger write-hygiene rule are
# compact single lines. Legacy pretty-printed records (4 known corrupt files, documented
# in retro/docs/upstream-drafts/01) are skipped by the per-line guard — an emitter that
# cannot see a corrupt record simply keeps looking for a clean one.
candidates = []
for project_dir in project_dirs:
    for dirpath, _, filenames in os.walk(project_dir):
        for name in filenames:
            if not name.endswith("-reviews.jsonl"):
                continue
            with open(os.path.join(dirpath, name), errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("---"):
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("skill") not in ALLOWED_SKILLS:
                        continue
                    commit = entry.get("commit")
                    if (
                        not isinstance(commit, str)
                        or commit in COMMIT_SENTINELS
                        or not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit)
                    ):
                        continue
                    candidates.append((parse_ts(entry.get("timestamp")), commit.lower(), entry))

if not candidates:
    die(
        "no review evidence in the ledger",
        f"no review/adversarial-review entry with a usable commit under {', '.join(sorted(os.path.basename(d) for d in project_dirs))}",
        "run the pre-ship review squad first, then re-run this script",
    )

# Newest first; entries with unparseable timestamps sort last rather than being dropped.
epoch = datetime.fromtimestamp(0, tz=timezone.utc)
candidates.sort(key=lambda item: item[0] or epoch, reverse=True)

for when, commit, entry in candidates:
    # Untruncated resolution: git sees the ledger's commit exactly as written. The
    # artifact then pins the RESOLVED full SHA — ambiguity-proof for the checker.
    rc, resolved = git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if rc != 0 or not resolved:
        continue  # not a commit in this repo (or an ambiguous prefix) — skip
    rc, _ = git("merge-base", "--is-ancestor", resolved, "HEAD")
    if rc != 0:
        continue
    os.makedirs("proof", exist_ok=True)
    evidence = {
        "schema_version": "1.0.0",
        "skill": entry.get("skill"),
        "commit": resolved,
        "timestamp": entry.get("timestamp"),
        "status": entry.get("status"),
        "emitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(OUT, "w") as handle:
        handle.write(json.dumps(evidence, separators=(",", ":")) + "\n")
    print(f"emit-review-evidence: wrote {OUT} (skill={evidence['skill']}, commit={resolved[:7]})")
    raise SystemExit(0)

die(
    "no ledger entry matches this branch",
    "review entries exist for this repo, but none has a commit that is HEAD or an ancestor of HEAD",
    "run the pre-ship review squad ON THIS BRANCH, then re-run this script",
)
PY

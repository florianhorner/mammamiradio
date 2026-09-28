#!/usr/bin/env bash
# read-preship-ledger.sh — repo-local review ledger reader for land-pr.sh.
#
# Emits gstack-review-read compatible JSONL (skill/commit/timestamp) followed by
# ---CONFIG---. The ledger scan is inlined here rather than shelled out to
# gstack-review-read: that binary lives in the gstack-upgrade clobber zone, and
# a product repo must not hard-depend on an unversioned third-party binary.
#
# Honors $GSTACK_HOME (default ~/.gstack). Exits 0 with only ---CONFIG--- when
# no ledger exists (land-pr treats that as "no local squad entry").
set -euo pipefail

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


rc, url = git("remote", "get-url", "origin")
if rc == 0 and url:
    repo_name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1])
else:
    repo_name = os.path.basename(os.getcwd())

if not os.path.isdir(LEDGER_ROOT):
    print("---CONFIG---")
    raise SystemExit(0)

project_dirs = [
    os.path.join(LEDGER_ROOT, name)
    for name in os.listdir(LEDGER_ROOT)
    if os.path.isdir(os.path.join(LEDGER_ROOT, name))
    and (name == repo_name or name.endswith(f"-{repo_name}"))
]
if not project_dirs:
    print("---CONFIG---")
    raise SystemExit(0)

entries = []
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
                    skill = entry.get("skill")
                    if skill not in ALLOWED_SKILLS:
                        continue
                    commit = entry.get("commit")
                    if (
                        not isinstance(commit, str)
                        or commit in COMMIT_SENTINELS
                        or not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit)
                    ):
                        continue
                    timestamp = entry.get("timestamp")
                    if not isinstance(timestamp, str):
                        timestamp = ""
                    entries.append(
                        (
                            parse_ts(timestamp),
                            {
                                "skill": skill,
                                "commit": commit.lower(),
                                "timestamp": timestamp,
                            },
                        )
                    )

epoch = datetime.fromtimestamp(0, tz=timezone.utc)
entries.sort(key=lambda item: item[0] or epoch, reverse=True)
for _, payload in entries:
    print(json.dumps(payload, separators=(",", ":")))
print("---CONFIG---")
PY

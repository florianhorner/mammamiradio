# Post-Cathedral Doc-Correctness Sweep — Recipe

**Date:** 2026-04-28
**Status:** Recipe (reusable for any future structural PR)
**Originally executed as:** PR #277 (`docs: post-cathedral correctness sweep`)
**Companion plan:** `docs/2026-04-28-cathedral-restructure.md` (the structural move this swept up after)

---

## When to run

Run a post-merge doc sweep after **any** PR that meets one of these triggers:

- Files moved across directories (≥10 files, e.g. cathedral subpackage move)
- Renames of frequently-referenced modules (e.g. `streamer.py` → `routes_listener.py`)
- Packaging surface changes (`pyproject.toml` `package-data`, `[tool.setuptools.packages.find]`)
- Public-API path changes (anything contributors write `from X` against)

Run **after** the structural PR merges, not before. The in-PR review and bot checks focus on the diff; a post-merge sweep covers the entire docs tree against the new reality. The cathedral PR (#275) caught most drift during review, but four stale references survived in active docs that the diff-scoped review never touched. That's the gap this recipe closes.

## What it is

An ad-hoc, time-boxed grep audit. Not a multi-step plan. The "plan" IS the recipe below.

Total time: **15-30 minutes** for a typical structural PR.

## The recipe

### Step 1 — Stale module path sweep

```bash
# Every flat module reference (uses cathedral nave list — adapt for future moves)
grep -rEn "mammamiradio/(ad_creative|audio_quality|capabilities|clip|config|context_cues|downloader|ha_context|ha_enrichment|models|normalizer|og_card|persona|playlist|producer|scheduler|scriptwriter|setup_status|song_cues|streamer|sync|track_rationale|track_rules|tts|voice_catalog)\.py" \
  --include="*.md" --include="*.py" --include="*.sh" --include="*.yml" --include="*.yaml" --include="*.toml" --include="*.html" \
  | grep -v "\.claude/\|\.venv/" \
  | grep -v "<dated-snapshot-globs>" \
  | grep -v "CHANGELOG.md\|ha-addon/mammamiradio/CHANGELOG.md"
```

Parameters that change between sweeps:
- The module list inside `(...)` — the set of modules whose paths changed
- The dated-snapshot globs to exclude — anything intentionally frozen as a point-in-time snapshot

### Step 2 — Stale HTML / static / asset path sweep

```bash
grep -rEn "mammamiradio/(admin|listener|live|regia)\.html|mammamiradio/static/[a-z_]+|mammamiradio/demo_assets" \
  --include="*.md" --include="*.py" --include="*.sh" --include="*.yml" --include="*.yaml" --include="*.html" \
  | grep -v "\.claude/\|\.venv/" \
  | grep -v "CHANGELOG.md\|ha-addon/mammamiradio/CHANGELOG.md"
```

### Step 3 — Markdown link target audit

```bash
# Links to root-level docs that no longer exist there
grep -rEn '\]\((ARCHITECTURE|OPERATIONS|TROUBLESHOOTING|HA_ADDON_RUNBOOK|DESIGN|ADMIN_PANEL_STANDARDS|CONDUCTOR|AGENTS|STABILIZATION_LOG|TODOS)\.md\)' \
  --include="*.md" \
  | grep -v "\.claude/" | grep -v "<dated-snapshot-globs>"
```

### Step 4 — Cross-doc reference verification

For each doc that has a "see also" or links to a sibling doc, verify the path resolves:

```bash
# REPO_MAP path verification — every code-fenced path must exist on disk
grep -oE "\`mammamiradio/[a-z_/]+\.(py|html|css|js|svg|json|md)\`" docs/REPO_MAP.md \
  | sort -u | sed 's/`//g' \
  | while read p; do [ -e "$p" ] && echo "OK $p" || echo "MISSING $p"; done \
  | grep -v "^OK"
```

### Step 5 — In-line code/doc sanity check (spot read)

Read these files top-to-bottom for tone/accuracy after a major move:

- `README.md` — first 25 lines must still pitch the product correctly
- `CONTRIBUTING.md` — local setup commands must still work
- `CLAUDE.md` — project structure section must match reality
- `docs/REPO_MAP.md` — every row must resolve
- `docs/architecture.md` — file map + path examples
- `docs/troubleshooting.md` — recovery paths must point at real files
- `docs/operations.md` — runtime path references
- `ha-addon/README.md` + `ha-addon/mammamiradio/DOCS.md` — operator-facing accuracy

## Decision rules — what to fix vs leave alone

### FIX (always)

- Active doc references stale paths that contributors will follow
- Operator-facing docs (HA addon, troubleshooting) with broken paths
- Markdown links that 404 on disk
- README / CONTRIBUTING / CLAUDE.md inconsistencies
- Per-line code or comment references in source files (especially `# See foo.py` style breadcrumbs)

### LEAVE AS-IS (always)

- **`CHANGELOG.md` historical release notes** — immutable per project rule. They describe state at the time of the release. Do not rewrite history.
- **`ha-addon/mammamiradio/CHANGELOG.md` historical entries** — same reason.
- **Dated snapshot docs** — `docs/2026-04-DD-...md` style audit notes that are point-in-time observations. The dated filename signals snapshot. Updating them rewrites history.
- **The plan doc that describes the move** — e.g. `docs/2026-04-28-cathedral-restructure.md` — it intentionally references the future state.

### FLAG (don't fix, but note)

- Pre-existing staleness predating this PR (e.g. references to long-deleted modules). Add a top-of-file note rather than fixing inline:
  ```markdown
  > **Path note (YYYY-MM-DD):** This doc was authored before the <move>. Module paths
  > below have been updated to nave-prefixed locations where the file still exists.
  > File X (removed in vY.Z.Z, PR #N) is mentioned for historical context only;
  > treat as design-history breadcrumb, not a live target.
  ```
  Done in #277 for `docs/designs/10x-vision-next.md` (which referenced `spotify_player.py` and `dashboard.html`, both deleted before the cathedral).

## Verification

After fixes, all of these must return empty (or only intentional skips):

```bash
# 1. Zero stale flat module refs in active docs
grep -rEn "mammamiradio/<flat-modules>\.py" --include="*.md" --include="*.py" \
  | grep -v "<intentional skips>"

# 2. Every REPO_MAP path resolves
# (see Step 4 above — must produce zero MISSING)

# 3. make check + tests still green
make check
.venv/bin/python -m pytest tests/ -q --no-cov
```

## Skipping the sweep

The sweep is **not required** when:
- The PR was logic-only (no file moves, no path changes)
- The PR was a single-file bug fix
- A doc-sync rule was already followed during the PR (you updated docs in the same commit as the code)

The sweep IS required when the structural PR had >20 files in the diff or changed packaging surfaces.

## Why this is a recipe, not a plan

Plans are for multi-step work where the order matters and decisions accumulate. The cathedral plan (#275 plan doc) needed 5 PRs sequenced over weeks.

A doc sweep is a single-PR mechanical exercise. The "plan" is the recipe; the artifact is the PR. Future sweeps replay this same recipe with the module list / dated-snapshot globs swapped for whatever moved.

## Outcomes from PR #277

- **5 fixes across 4 files** caught:
  - `docs/architecture.md:79` — `demo_assets/sfx/studio/` → `assets/demo/sfx/studio/`
  - `docs/architecture.md:103` — `models.py`, `capabilities.py` → `core/`-prefixed
  - `docs/troubleshooting.md:100` — `voice_catalog.py` → `audio/`-prefixed
  - `docs/designs/10x-vision-next.md` — header note + cathedral-moved paths bumped
  - `scripts/coverage-ratchet.py:57` — comment example with nave-prefixed path

- **Zero false positives** — every grep hit was either a real fix or an intentional skip (snapshot, changelog history).

- **Time:** ~25 minutes from "where are the stale refs?" to PR opened.

## Reusable for

- PR 5 (split `web/streamer.py`) — use this recipe after merge
- PR 6 (split `hosts/scriptwriter.py`) — same
- Any future structural move with >20 files in the diff

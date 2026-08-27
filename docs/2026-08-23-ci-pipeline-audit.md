# CI/CD pipeline failure-pattern audit

> **Facts only.** This document records validated findings from repository,
> GitHub, and Conductor session evidence. It does not prescribe remediation.
> Remediation is outside this historical audit's scope.

**Evidence cutoff:** `origin/main` at `6fc4a851` (2026-08-23).<br>
**Audit scope:** mammamiradio Conductor corpus (631 workspaces, 1,829 sessions,
~1.1M message rows, 2026-04-03 through 2026-08-23), GitHub API queries on merged
PRs, and read-only inspection of landing scripts and workflows.

---

## Scope and methods

1. **Conductor session corpus** — searched for preship conflicts, landing
   failures, orphaned pins, and unresolved-review mentions across all mammamiradio
   workspaces (including archived).
2. **Git history** — enumerated every first-parent main commit that touched
   `proof/preship-review.json`; ran ancestry checks against pinned commits.
3. **GitHub API** — GraphQL queries on merged PR review threads; paginated scan
   of 766 merged PRs for unresolved, non-outdated threads.
4. **Script/workflow inspection** — `land-pr.sh`, `check-preship-evidence.sh`,
   `emit-review-evidence.sh`, `pr-queue-status.sh`, `check-merge-gate.sh`,
   `preship-evidence.yml`, `quality.yml`.

Conversation statements were treated as leads and corroborated against canonical
Git/GitHub evidence before inclusion.

---

## Evidence anchors and reproduction

This is a historical snapshot. “Current” in quoted source material means current
at the cutoff below, not current when this document is read. GitHub review-thread
state and the Conductor database are mutable, so a later rerun can legitimately
produce different totals.

| Evidence | Observation completed | Immutable output recorded by this audit |
|----------|-----------------------|-----------------------------------------|
| Repository | `2026-08-23T00:41:40Z` | `origin/main` = `6fc4a851127aac8fa2b93b9d87d50330c52f9892` |
| Conductor corpus | `2026-08-23T01:02:51.229Z` | 631 workspaces; 1,829 sessions; 1,099,023 message rows |
| All merged-PR review threads | `2026-08-23T01:20:27.119Z` | 766 PRs; 276 PRs with 893 unresolved, non-outdated threads |
| Recent merged-PR cohort | `2026-08-23T01:20:08.336Z` | PRs #882–#1014: 36/100 PRs with 107 such threads; 61 CodeRabbit Major/Critical threads |

The Conductor totals came from this exact read-only query. The repository ID is
the `mammamiradio` row in the local Conductor database's `repos` table.

```bash
sqlite3 -readonly -header -column \
  "$HOME/Library/Application Support/com.conductor.app/conductor.db" \
  "SELECT count(*) AS workspaces FROM workspaces WHERE repository_id='2be936a0-e847-4ca6-a2be-c09037abe7ec';
   SELECT count(*) AS sessions FROM sessions s JOIN workspaces w ON w.id=s.workspace_id WHERE w.repository_id='2be936a0-e847-4ca6-a2be-c09037abe7ec';
   SELECT count(*) AS messages FROM session_messages sm JOIN sessions s ON s.id=sm.session_id JOIN workspaces w ON w.id=s.workspace_id WHERE w.repository_id='2be936a0-e847-4ca6-a2be-c09037abe7ec';"
```

The recent-cohort result came from this exact GraphQL query and filter. “Active”
means `isResolved == false` and `isOutdated == false`; Major/Critical means a
CodeRabbit-authored first comment containing its `_🟠 Major_` or `_🔴 Critical_`
label.

```bash
gh api graphql \
  -f query='query($owner:String!,$name:String!){repository(owner:$owner,name:$name){pullRequests(first:100,states:MERGED,orderBy:{field:CREATED_AT,direction:DESC}){nodes{number mergedAt reviewThreads(first:100){nodes{isResolved isOutdated comments(first:1){nodes{author{login}createdAt body}}}}}}}}' \
  -F owner=florianhorner -F name=mammamiradio \
  --jq '
  [.data.repository.pullRequests.nodes[] as $pr |
    [$pr.reviewThreads.nodes[] |
      select(.isResolved == false and .isOutdated == false) |
      {author:(.comments.nodes[0].author.login // ""),
       createdAt:.comments.nodes[0].createdAt,
       body:(.comments.nodes[0].body // "")}] as $u |
    {number:$pr.number,
     mergedAt:$pr.mergedAt,
     unresolved:($u|length),
     coderabbit:([$u[]|select(.author=="coderabbitai")]|length),
     cr_major:([$u[]|select(.author=="coderabbitai" and
       (.body|test("_🟠 Major_|_🔴 Critical_")))]|length),
     after_merge:([$u[]|select(.createdAt > $pr.mergedAt)]|length)}
  ] as $rows |
  {range:[($rows|map(.number)|min),($rows|map(.number)|max)],
   cohort_prs:($rows|length),
   prs_with_unresolved:([$rows[]|select(.unresolved>0)]|length),
   unresolved_threads:([$rows[].unresolved]|add),
   unresolved_coderabbit_threads:([$rows[].coderabbit]|add),
   prs_with_cr_major:([$rows[]|select(.cr_major>0)]|length),
   cr_major_or_critical_threads:([$rows[].cr_major]|add),
   prs_with_postmerge_threads:([$rows[]|select(.after_merge>0)]|length),
   postmerge_threads:([$rows[].after_merge]|add)}'
```

The all-merged-PR totals used the same active-thread predicate while paginating
every merged PR to `hasNextPage == false`. This minimal reproduction command
uses the same query fields, pagination, and counting predicate:

```bash
python3 - <<'PY'
import json
import subprocess
import time

query = r'''
query($after:String) {
  repository(owner:"florianhorner", name:"mammamiradio") {
    pullRequests(first:50, after:$after, states:MERGED,
                 orderBy:{field:CREATED_AT, direction:DESC}) {
      nodes {
        number
        reviewThreads(first:100) {
          totalCount
          nodes { isResolved isOutdated }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}'''

after = None
pull_requests = []
while True:
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    if after:
        command += ["-f", f"after={after}"]
    for attempt in range(5):
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode == 0:
            break
        time.sleep(attempt + 1)
    result.check_returncode()
    page = json.loads(result.stdout)["data"]["repository"]["pullRequests"]
    pull_requests.extend(page["nodes"])
    if not page["pageInfo"]["hasNextPage"]:
        break
    after = page["pageInfo"]["endCursor"]

active = [
    (pull_request["number"], thread)
    for pull_request in pull_requests
    for thread in pull_request["reviewThreads"]["nodes"]
    if not thread["isResolved"] and not thread["isOutdated"]
]
truncated = [
    pull_request["number"]
    for pull_request in pull_requests
    if pull_request["reviewThreads"]["totalCount"] > 100
]
print(json.dumps({
    "merged_prs": len(pull_requests),
    "active": len(active),
    "pr_count": len({number for number, _ in active}),
    "truncated": truncated,
}, indent=2))
PY
```

Recorded output: `{"merged_prs":766,"active":893,"pr_count":276,
"truncated":[]}`. The empty `truncated` list establishes that the
`reviewThreads(first:100)` bound omitted no thread-bearing PR in this snapshot.

---

## Validated examples

### PR #1013 (keepsakes) — squash orphans evidence; unresolved bot threads at merge

- Squash-merged at `2026-08-23T00:41:40Z` to `6fc4a851`.
- `proof/preship-review.json` on main pins `8915e7c1`, status `issues_found`.
- `git merge-base --is-ancestor 8915e7c1 6fc4a851` is **false** (squash replaced
  eight branch commits with one).
- GitHub shows **four** unresolved, non-outdated review threads at merge time,
  including CodeRabbit Major findings (full-buffer allocation, unchecked `int()`
  conversion, delete validation contract, missing fsync on keepsake directory).
- Quality check completed two seconds before merge; aggregate rollup was green.
- `reviewDecision` was null; ruleset has zero required approvals and conversation
  resolution disabled.

### PR #1012 (Jamendo) — fixed-path evidence collision

- Merge commit `102d3abb` records conflict in `proof/preship-review.json` when
  merging `origin/main` into the feature branch.
- Final head predated an unresolved CodeRabbit Major thread; PR merged 19 minutes
  after that comment.

### PR #979 (audio pack) — squash orphans generator revision (precedent)

- Squash merge `5f3e44c1` made generator revision `7a903df2` unreachable from main.
- `test_public_pack_provenance_and_attribution_ledger_are_valid` failed on trunk
  and inherited PRs until annotated tag `provenance/audio-pack-7a903df2` restored
  blob reachability.
- Different checker semantics than preship evidence: audio validator reads blobs
  from a resolvable revision; it does not require main-ancestor relationship.

### Tagging `8915e7c1` would not repair preship check on main

A tag preserves object reachability but does not alter commit ancestry.
`check-preship-evidence.sh` requires `merge-base --is-ancestor` of the pinned
commit against the target. Tagging is appropriate for audio-pack blob resolution,
not for preship evidence validity after squash.

---

## Structural failures at the evidence cutoff

### 1. Review-thread resolution is not a merge condition

Active GitHub ruleset `require-pull-request-before-merging` has
`required_review_thread_resolution: false`, `required_approving_review_count: 0`,
`dismiss_stale_reviews_on_push: false`.

`land-pr.sh` and `pr-queue-status.sh` do not query review threads or
`reviewDecision`.

**Measured (766 merged PRs):** 276 PRs retain at least one unresolved,
non-outdated thread (893 threads total). In the 100 most recently created merged
PRs (#882–#1014): 36 PRs, 107 threads; 61 CodeRabbit threads labeled Major or
Critical.

### 2. Merge-on-green can outrun semantic review

Landing scripts treat green required checks as sufficient. Documented delivery
retrospective (2.18 cohort) recorded ten of twenty core PRs merging within three
seconds of quality completion.

### 3. Evidence records review presence, not outcome

`emit-review-evidence.sh` writes `status` (`clean`, `issues_found`,
`issues_open`). Neither `check-preship-evidence.sh` nor `land-pr.sh` reads it.

Of 25 mainline commits that changed `proof/preship-review.json` since introduction
(#915): 15 `clean`, 9 `issues_found`, 1 `issues_open` — all landable.

### 4. Ancestor-plus-grace is looser than documented contract

`land-pr.sh` accepts a squad entry whose commit is an ancestor of PR head plus a
600-second grace window on newest commit date. `CLAUDE.md` describes exact-head
freshness.

Realized case: PR #982 evidence pinned pre-feature parent `5f3e44c1` while the
559-line feature commit landed 103 seconds later inside grace.

### 5. Post-`update-branch` head is not re-reviewed

`land-pr.sh` runs `squad_check` before `gh pr update-branch`, replaces head after
update, arms merge without re-running squad or evidence checks. Test case 3 in
`test_land_pr.sh` explicitly requires arming on an arbitrary post-update head.

### 6. Squash systematically invalidates committed evidence on main

Checker requires pinned commit to be ancestor of target. Landing always uses
`--squash`.

Of 25 mainline artifact landings: 24 fail ancestry against their own landing
commit; sole valid case pinned immediate pre-change parent (#982).

Current main (`6fc4a851`) pins `8915e7c1` — not an ancestor.

### 7. Fixed evidence path is a merge-conflict hotspot

`proof/preship-review.json` is overwritten by every `/ship` on every branch.

- **Git:** at least eight merge commits explicitly list this file in conflict
  messages.
- **Conductor:** exact string `CONFLICT (content): Merge conflict in
  proof/preship-review.json` appears 21 times across 15 sessions in 11 workspaces
  (before this audit session).

### 8. Report-only and fail-open controls surface as success

- `preship-evidence.yml` converts invalid/missing evidence to `::warning::` and
  exits 0 (phase 1 report-only by design).
- `require-preship-squad.sh` fails open on internal errors; unavailable in Codex.
- `check-merge-gate.sh` exits 0 in CI (cannot read branch protection).
- `quality.yml` coverage-ratchet push failure is a successful warning, not a job
  failure.

### 9. Landing proves arming, not merge completion

`land-pr.sh` exits after `gh pr merge --auto`. It does not poll for `MERGED`,
record squash SHA, or inspect post-merge workflows. Multi-PR argument loop arms
sequentially without waiting for prior merges.

### 10. Post-merge checks are structurally late

`Build HA Addon` runs on push to main with path filters; runbook states it cannot
be a required PR check. Add-on image failures are discovered after merge.

### 11. Merge-gate drift detection is local-only

`check-merge-gate.sh` skips in CI. Conversation-resolution setting is not asserted.
Drift is caught at next local `make pre-release`, not at ship time.

### 12. Local `/ship` hook is runtime-incomplete

Pre-ship squad hook is Claude-only. Server-side replacement is report-only until
explicit phase-2 flip.

---

## Historical signatures (already fixed or retired)

These incidents informed pattern classification; controls may have been hardened
since occurrence:

| Incident | Pattern |
|----------|---------|
| #979 audio-pack + tag repair | Squash orphans self-pinned revision |
| #384 / #476 / #485 Edge auto-bump chain | External-state race, self-waiting automation |
| #993 Dependabot nudge + `UNKNOWN` merge state | Silent no-op while PRs parked |
| #567 stale-base near-miss | Hand-rolled integration vs strict up-to-date |
| #991 grouped Dependabot commit-lint | Representation-driven false red |
| #996 PCRE in POSIX ERE lint list | Silent-green gate (4 months) |
| #1006 / #1009 explainer workflow path filters | Workflow self-blindness |
| #572 addon smoke wrong runtime mode | Wrong acceptance surface |
| #924 unpublished advertised version | Release truth drift (74/76 days) |
| #639 / #633 coverage ratchet | Guard self-weakening |
| #972 report-only half-applied | Scope split across pipeline stages |
| #829 → #871 continuity review escape | Unresolved Major merged, follow-up required |

---

## Numbered inventory

1. Review-thread resolution is not a merge condition — 36/100 recent merged PRs
   retain 107 active threads; 61 bot-labeled Major/Critical.
2. Merge-on-green outruns semantic review — documented sub-minute merges after
   quality in 2.18 cohort.
3. Evidence records presence, not outcome — 10/25 landed artifacts non-clean.
4. Ancestor-plus-grace can bless unreviewed feature commits — realized on #982.
5. Updating a behind branch creates an unchecked replacement head — tested and
   required by current `test_land_pr.sh` case 3.
6. Squash and evidence ancestry are incompatible on main — 24/25 artifacts fail
   post-landing ancestry check.
7. Fixed evidence filename is a deterministic collision point — 21 recorded
   Conductor conflict outputs; 8+ git merge commits.
8. Report-only and fail-open controls still surface as successful checks.
9. Landing proves auto-merge arming, not merge completion or post-merge health.
10. Multi-PR landing arms sequentially without waiting for prior merge completion.
11. Proof gates brittle to Markdown, checkbox, title, and literal-version
    representation (historical).
12. Timing assertions and runner load create nondeterministic reds (historical;
    recent example: `test_time_check_render_trace_records_tts_and_mix` under xdist).
13. Path filters allowed workflows to avoid testing or triggering themselves
    (historical explainer workflows).
14. Smoke checks validated wrong runtime, artifact, or listener surface
    (historical addon smoke).
15. External GitHub/registry state produced silent no-ops and automation races
    (historical Dependabot nudge, Edge bump).
16. Release and coverage metadata drifted while checks remained green (historical
    advertised-version, changelog-head, coverage-floor incidents).

---

## Boundary

This document is **read-only analysis**. It does not authorize merges, ruleset
changes, workflow flips to blocking, or code changes.

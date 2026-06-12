# Runbook — HA upstream watcher

**Job:** catch Home Assistant changes that touch mammamiradio's HA surface
(the `media_player` entity, the add-on, the planned `custom_components/` integration,
the Music Assistant provider) **before they reach a stable release** — so an
opportunity like the 2026.6 entity-first card picker, or a breaking change to
`media_player`/`config_flow`, is known weeks early instead of after it ships.

Tool: `scripts/ha-watch.py` (pure stdlib, no extra deps). Tests:
`tests/scripts/test_ha_watch.py`.

## What it polls

Ranked by lead time (highest-signal first):

| Source | Feed | Lead time |
|--------|------|-----------|
| Developer blog | `developers.home-assistant.io/blog/rss.xml` | days–months |
| Core `breaking-change` PRs | `api.github.com/repos/home-assistant/core/issues?labels=breaking-change&state=open` | weeks |
| Frontend `breaking-change` PRs | same on `home-assistant/frontend` | weeks |
| Architecture discussions | `github.com/home-assistant/architecture/discussions.atom` | weeks–months |
| Main release blog | `home-assistant.io/atom.xml` | GA day |

It keeps only items whose title/body mentions the HA surface (keyword list in
`scripts/ha-watch.py:KEYWORDS` — `media_player`, `supported_features`,
`getentitysuggestion`, `card picker`, `config_flow`, `hacs`, `media_source`,
`music assistant`, `add-on`/`supervisor`/`ingress`, `/api/states`, …) and
reports only items not in its seen-state file.

## Manual use

```bash
scripts/ha-watch.py            # human summary of NEW relevant items
scripts/ha-watch.py --json     # machine-readable (for a scheduler)
scripts/ha-watch.py --dry-run  # report without persisting seen-state
```

Seen-state defaults to `~/.cache/mammamiradio/ha-watch-state.json`
(override with `--state PATH` or `MAMMAMIRADIO_HA_WATCH_STATE`).
Set `GITHUB_TOKEN`/`GH_TOKEN` to lift the GitHub API rate limit (optional).

**Prime once before the first scheduled run** so it doesn't dump the whole
history on day one:

```bash
scripts/ha-watch.py >/dev/null   # populates state with everything current
```

After priming, each run shows only what's genuinely new.

## Scheduled form (weekly)

Run weekly (Tuesdays — betas open ~7 days before the first-Wednesday GA). The
recommended form factor is a scheduled agent (via `/schedule`) rather than a
bare cron, so it can judge true relevance and open a narrow GitHub issue per
genuine hit. Suggested agent loop:

1. `scripts/ha-watch.py --json --dry-run` to list candidate new items.
2. For each, judge whether it actually affects mammamiradio's HA surface
   (the keyword filter is recall-biased; the agent adds precision).
3. For a genuine hit with no existing tracking issue, open one: narrow title,
   the link, and one line on why it touches us.
4. Dedup against existing issues (label `ha-upstream`) rather than the local
   state file, since a cloud routine starts from a fresh checkout each run.

A no-op week opens nothing. The watcher never touches the running station.

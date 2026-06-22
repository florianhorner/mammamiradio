# Repo-Local Agent Rules

This file supplements the global instructions for the `mammamiradio` repository.

## Repo Profile

- Stack: Python, FastAPI, Docker, Bash lifecycle scripts, `pyproject.toml` versioning
- Product: `mammamiradio`, an AI-powered Italian radio station with a Home Assistant add-on

## Working Rules

- Conventional commits only: `feat:`, `fix:`, `chore:`, `ci:`, `deps:`
- Never modify `.context/` runtime state
- If Conductor lifecycle hooks change, update the `scripts/conductor-*.sh` files (and your Conductor `.conductor/settings.toml`) in the same change
- On version bumps, keep `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md` in sync
- In engineering reviews, when presenting multiple options, explain the tradeoffs without framing one as the choice the user should automatically take

## Integration Trains

- `Train/Listener QS` lives on `train/listener-qs` and uses `origin/main` as
  its base. Feature worktrees that target this train must hand off through
  `docs/listener-qs-train.md`.

## Fictional Ad Lab

Use `scripts/fictional_ad_lab.py` when exploring new fake sponsors for the
station. The lab is artifact-first: it generates candidates, campaign spines,
collision-check queries, trigger ideas, and TOML snippets under an output
directory, but it never edits `radio.toml`.

Cloud-friendly run:

```bash
python scripts/fictional_ad_lab.py --output-dir /opt/cursor/artifacts/ad-lab
```

Optional taste-audition run:

```bash
python scripts/fictional_ad_lab.py --output-dir /opt/cursor/artifacts/ad-lab --render-edge-specs
```

Before committing a candidate, web-check every finalist in
`brand_collision_checks.md`. A real company, product, registered mark, or
lookalike fails the brand-safety rule. Treat `trigger_ideas.md` as advisory for
the BFF/load view: hooks are labels for later operator judgment, not automatic
scheduling rules. Do not fire spec concepts during queue rescue, startup warmup,
or first-audio recovery.

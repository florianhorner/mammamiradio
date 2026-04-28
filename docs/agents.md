# Repo-Local Agent Rules

This file supplements the global instructions for the `mammamiradio` repository.

## Repo Profile

- Stack: Python, FastAPI, Docker, Bash lifecycle scripts, `pyproject.toml` versioning
- Product: `mammamiradio`, an AI-powered Italian radio station with a Home Assistant add-on

## Working Rules

- Conventional commits only: `feat:`, `fix:`, `chore:`, `ci:`, `deps:`
- Never modify `.context/` runtime state
- If Conductor lifecycle hooks change, update `conductor.json` and the related `scripts/conductor-*.sh` files in the same change
- On version bumps, keep `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md` in sync
- In engineering reviews, when presenting multiple options, explain the tradeoffs without framing one as the choice the user should automatically take

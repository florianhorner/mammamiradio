# TODOs

## Automated tool version updates
Add Dependabot or Renovate to keep ruff, mypy, and pre-commit hook versions current.
Pre-commit has `pre-commit autoupdate` for hook revs. Dependabot can handle pinned
versions in `.github/workflows/quality.yml`. Both are ~5 min to configure.

**Why:** Pinned versions (ruff==0.9.10, mypy==1.15.0) go stale. New versions bring
bug fixes and better lint rules. Without automation, these pins rot silently.

**Where to start:** Add `.github/dependabot.yml` with pip ecosystem config, or run
`pre-commit autoupdate` periodically.

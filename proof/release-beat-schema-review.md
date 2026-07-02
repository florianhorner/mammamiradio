# /code-review — issue #728 (release_beat_schema single source of truth)

Run: florianhorner-fleet-single-source-of-truth-for-the-release-b-1782980953
Files: mammamiradio/core/release_beat_schema.py, scripts/validate-release-beat.py, mammamiradio/release_campaign.py, tests/core/test_release_beat_schema.py, CHANGELOG.md

No P0/P1 findings. 2 P3 (informational, not gating):

1. `release_beat_schema.py:48` — the shared `ALLOWED_KEYS` gate widens the validator's accepted key set from 12 to 19 (adds the 7 runtime-only keys the old validator rejected as unknown). Beneficial (aligns validator with what the runtime loader actually reads) and low risk (default manifest is absent/disabled, so no live manifest is affected), but the CHANGELOG's "Refactored" line doesn't call out that the validator's accepted-key surface changed.
2. `release_campaign.py:30` — `RELEASE_BEAT_RUNTIME_KEYS = RUNTIME_CONSUMED_KEYS` is a newly-introduced module constant with no consumer anywhere in the repo (grep-confirmed). Not wired up; candidate for removal or future use.

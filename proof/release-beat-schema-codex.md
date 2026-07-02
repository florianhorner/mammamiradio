# codex review — issue #728 (release_beat_schema single source of truth)

Run: florianhorner-fleet-single-source-of-truth-for-the-release-b-1782980953

First pass found one **P2**: the unified `ALLOWED_KEYS` widened the validator to admit `title` and `copy_guidance` — both free-text fields that reach the on-air prompt — without routing them through the existing listener-safety term scan that every other free-text release-beat field gets.

Fix applied in-scope: added `_validate_scalar_text()` (mirrors the existing `_validate_text_list` listener-safety scan) and wired it to `title`/`copy_guidance`, respecting the manifest's existing `listener_safe_terms` opt-in. Added 2 regression tests.

Re-verify after fix: **0 P0/P1/P2 findings**. Full suite green (3637 passed, 2 skipped — 2 more than the pre-fix run, from the new regression tests).

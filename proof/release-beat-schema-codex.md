# codex review follow-up — issue #728

Run: local follow-up on PR #736 after pre-landing review findings.

Fix applied in scope:
- `title` and `copy_guidance` now get the same one-line, max-length, placeholder-copy, and listener-safety checks as release-beat list copy.
- `forbidden_terms` now gets the same list shape and copy-hygiene checks as the legacy `avoid` alias.
- `max_airings`, `campaign_window_seconds`, `min_seconds_between_airings`, and `min_segments_between_airings` now require integer values inside explicit validator ranges instead of passing through to runtime coercion.

Re-verify after fix: 0 known P0/P1/P2 findings in the patched validator/test scope.
Proof: `proof/release-beat-schema-tests.txt`.

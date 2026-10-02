# Stabilization and release exceptions

Record unresolved release-gate exceptions here, with their scope and the evidence
still required. Keep current operational requirements in the release runbook.

## Release gate exceptions

| Release | Gate | Status | Rationale |
|---------|------|--------|-----------|
| v2.18.0 | Backup canary: restore into a disposable Home Assistant installation | Waived | No disposable Supervisor installation was available for that release. HA backups need Supervisor; a plain container image cannot restore one. The live-copy half of the gate (SQLite integrity + ledger parse on a backup taken while playing) still applies. The round trip — a valid archive actually bringing a station back up — is unverified for this release. Stand up a disposable instance before the next backup-contract change. |

A local container lab can validate HTTP and UI behavior, but does not close the
Supervisor backup-restoration exception.

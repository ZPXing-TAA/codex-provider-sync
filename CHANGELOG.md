# Changelog

## 0.2.1 - 2026-08-16

- Package the tool as `codex-provider-sync` with a `codex-switch` CLI.
- Add a shell-wrapper installer to avoid a macOS provenance execution issue.
- Validate root-level TOML provider configuration before switching.
- Warn before relabeling rollout files that contain provider-bound encrypted history.
- Reconcile both known thread databases and rebuild the desktop sidebar catalog.
- Add SQLite-consistent backups, automatic rollback, backup listing, and restore.
- Add isolated tests for success, idempotency, failure recovery, and restore flows.

# Changelog

## 0.3.1 - 2026-10-08

- Run provider-aware response-item normalization during rollout migration instead of only changing `session_meta.model_provider`.
- Preserve portable messages and visible reasoning summaries while removing opaque reasoning, tool continuation state, and provider-generated IDs.
- Require an explicit continuation-compatibility assertion before preserving native state, even when provider names match.
- Record the rollout files' actual source-provider counts in backup manifests.
- Report provider-bound rollout and response-item counts in `status` output.

## 0.3.0 - 2026-09-28

- Add a provider-aware Responses API history normalizer.
- Keep provenance separate from request items.
- Strip cross-provider IDs, drop opaque continuation state, and remove tool pairs together.
- Validate same-provider reasoning namespaces and tool relationships before serialization.
- Add regression coverage for legacy contaminated sessions and repeated provider switches.

## 0.2.1 - 2026-08-16

- Package the tool as `codex-provider-sync` with a `codex-switch` CLI.
- Add a shell-wrapper installer to avoid a macOS provenance execution issue.
- Validate root-level TOML provider configuration before switching.
- Warn before relabeling rollout files that contain provider-bound encrypted history.
- Reconcile both known thread databases and rebuild the desktop sidebar catalog.
- Add SQLite-consistent backups, automatic rollback, backup listing, and restore.
- Add isolated tests for success, idempotency, failure recovery, and restore flows.

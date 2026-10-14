# Changelog

## 0.5.0 - 2026-10-14

- Make `sync` follow the provider already selected in CC Switch. Align built-in OpenAI via `openai_base_url` and configured `custom` / `openai-http` aliases without changing stored chat provider IDs or response items.
- Clear managed relay routing and stale tokens when returning to the official service. Keep `auth.json` untouched; reject mismatched global keys and unsupported custom authentication before restarting or writing.
- Recognize both Codex and ChatGPT desktop app names for safe shutdown/reload.
- Add an isolated native app-server loopback probe covering resumed OpenAI routing, dummy authentication, preserved original messages, and opaque reasoning.
- Prefer the `openai`/`custom` pair for automatic switching when extra providers such as `openai-http` exist. Treat the existing `openai-http` starting provider as the official side of that pair.
- Leave the app running and skip backups when the requested provider is already active.
- Document the CC Switch → `sync` workflow and distinguish route correction from native encryption/lineage compatibility.

## 0.4.0 - 2026-10-14

This is a safety-focused breaking change. **Do not use versions <= 0.3.2 on real Codex data.** They could delete provider-native history, relabel unrelated thread metadata, and overwrite newer state during restore. Preserve all existing recovery backups and upgrade before attempting recovery.

- Change `switch` to update only the root default provider. It backs up config, every known database, and all rollout files; it no longer rewrites history, thread provenance, or sidebar indexes.
- Allow `switch` with no provider argument to auto-toggle only when exactly one OpenAI and one custom provider are configured. Ambiguous provider setups require an explicit target.
- Change `sync` to reconcile indexes from per-rollout provider provenance, retain mixed-provider state, and leave unmatched catalog rows intact.
- Make restore additive: restore missing rollout files/DB rows while preserving current data; recover known normalized snapshots only when the original and appended suffix are unambiguous; report and leave conflicts untouched.
- Restore only the root provider setting instead of replacing the entire current config. Create a full safety backup of current data before restore.
- Disable backup pruning by default. Explicit `--keep` applies only to v4-format switch/sync backups and protects legacy/recovery backups.
- Keep history normalization out of CLI mutation paths. Switching providers does not make native history portable or guarantee cross-provider continuation/lineage repair.

## 0.3.2 - 2026-10-08

- Make `codex-switch sync` automatically detect the target from the active `config.toml` provider and proceed without a confirmation prompt.
- Keep `--yes` as a hidden no-op for compatibility with existing scripts.

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

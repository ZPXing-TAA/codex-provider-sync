# Codex Provider Sync

[简体中文](README.zh-CN.md)

`codex-switch` is an unofficial macOS tool for Codex Desktop and Codex in ChatGPT. **Select a service in CC Switch, then run `codex-switch sync`.** It aligns existing chat routes and local indexes without compressing, deleting, or rewriting native conversation history.

Use the same command in either direction: no provider argument and no `--yes`. The command closes and reopens the desktop app; finish other running tasks first.

> [!CAUTION]
> **Stop using releases <= 0.3.2 for real data.** Those versions could normalize/delete provider-native response items, relabel every thread as one provider, and restore an old database snapshot over newer chats. Keep every existing directory under `~/.codex/recovery_backups/`, upgrade first, and do not run an old `sync` or `restore` while troubleshooting. This project cannot recreate history already removed by an old release; recover from the best untouched backup instead.

## What changed in 0.5

- `sync` follows the configuration already selected by CC Switch. It does not toggle again or change `auth.json`. Route alignment covers `openai`, `custom`, and `openai-http`.
- In this workflow, built-in `openai` means the official service and clears `openai_base_url`. Use `custom` with an explicit `base_url` for relays. Missing `custom` / `openai-http` definitions still needed by stored chats are recreated as aliases of the selected service.
- For a relay, the supported `openai_base_url` setting routes existing OpenAI chats to the selected endpoint. Configured `custom` / `openai-http` aliases follow the same service. Switching back clears the tool's relay URL and replaces stale relay tokens in inactive aliases. Stored chat provider IDs remain unchanged.
- This updates those routing definitions in config, including inactive aliases. Let CC Switch manage service definitions and login; select the next service there before syncing again. The complete previous config is backed up.
- A custom endpoint requires a matching CC Switch API-key login in `auth.json`. Independent `env_key`, token commands, or additional request headers cannot be mirrored into built-in OpenAI routing; the command rejects these before making changes.
- App lifecycle handling recognizes both `Codex.app` and `ChatGPT.app` so restarted sessions load the new config.

The data protection introduced in 0.4 remains:

- `switch` changes only the root `model_provider` and creates a complete backup of config, databases, and all rollout files. It does **not** rewrite JSONL response items, reasoning, compaction, tool state, thread provenance, or the sidebar index.
- `switch` with no provider argument toggles `openai` and `custom`, even when additional providers such as `openai-http` are configured. From `openai-http`, it switches to `custom`. Without a provider named `custom`, auto-toggle requires exactly one non-OpenAI provider. Otherwise pass the provider explicitly.
- Index reconciliation still reads provider provenance per rollout, repairs only matching database rows, preserves mixed-provider installations, and keeps unmatched catalog rows.
- `restore` is additive: missing rollouts and missing database rows are copied in while current/new data is retained. A known exact-prefix or legacy-normalized snapshot may be repaired with its canonical original plus an unambiguous appended suffix. Ambiguous conflicts are left untouched and reported.
- Restoring a backup changes only the root provider setting; it does not replace the whole current config. Every restore first makes a safety backup containing all current rollouts.
- No backups are pruned by default. `--keep N` only applies when explicitly requested and never deletes legacy/recovery backups.

The history normalizer remains a library boundary for a host that is deliberately serializing a request. The CLI does not invoke it to mutate persisted history, and it is not an automatic request serializer.

## Important limitation

This fixes routing mismatches such as sending a relay key to the official endpoint; it does not guarantee native-state compatibility between services. Response IDs, encrypted reasoning, compaction, and continuation lineage may depend on the original account, endpoint, or transport. The tool preserves them rather than deleting them to force continuation.

Existing `openai` chats retain the built-in provider's transport, including WebSocket. HTTP-only relays may still be incompatible. For encrypted-content or lineage errors, retain originals/backups and return to the original service; do not use old normalization or restore commands. A successful `sync` report confirms local configuration/index processing, not a successful real-service conversation.

## Storage and status

Codex stores conversation data in JSONL rollouts, thread databases (`state_5.sqlite` and `sqlite/state_5.sqlite`), and a sidebar catalog (`sqlite/codex-dev.db`). `status` reports these layers independently:

- `config_provider` is the default for new requests, not the owner of every old chat.
- `rollouts` counts files, including active, archived, empty, and internal sessions.
- `databases` counts indexed thread records; two database locations do not imply two copies of each conversation.
- `catalog` counts sidebar records, which need not include every rollout or indexed thread.

Different totals are not by themselves evidence of data loss. Mixed provider counts are expected after a safe switch; making every count say `custom` is not a repair goal.

~~~bash
codex-switch status
codex-switch status --json
~~~

## Install

Requires macOS, Codex Desktop, and Python 3.10 or newer. Custom providers must already be configured in `~/.codex/config.toml`.

~~~bash
git clone git@github.com:ZPXing-TAA/codex-provider-sync.git
cd codex-provider-sync
./install.sh
codex-switch --version
~~~

The installer creates an isolated environment under `~/.local/share/codex-provider-sync` and installs `codex-switch` under `~/.local/bin`. Ensure that directory is on `PATH`. Uninstalling the command does not remove recovery backups:

~~~bash
./uninstall.sh
~~~

To upgrade an existing checkout, pull the new release and rerun `./install.sh`. CC Switch route following requires `codex-switch --version` to report 0.5.0 or later.

## Commands

| Command | Purpose |
| --- | --- |
| `codex-switch status [--json]` | Inspect provider provenance and index counts. |
| `codex-switch switch [provider]` | Toggle `openai`/`custom`, or change the root provider to an explicit target. |
| `codex-switch sync` | Follow the active CC Switch route and reconcile indexes without rewriting history. |
| `codex-switch backups` | List recovery backups. |
| `codex-switch restore [path]` | Additively restore a backup (latest by default), preserving current/new data. |

Options:

- `--codex-home PATH` (global, before the command) operates on a test Codex home.
- `--no-open` leaves Codex closed after the operation. Writes normally quit Codex first.
- `--keep N` opts into pruning only v4-format `switch`/`sync` backups; legacy and recovery backups stay protected.

Select the destination in Codex or CC Switch, then run:

~~~bash
codex-switch sync
~~~

OpenAI is built in; other providers must already be declared in `~/.codex/config.toml`. The tool does not create credentials.

**Do not run `switch` after switching in CC Switch.** It changes the default provider again without switching authentication. `switch [provider]` remains available for manually managed configurations, not as a replacement for CC Switch login changes. Selecting the already-active provider does not restart or back up the app.

If `401 / invalid_api_key` persists, check whether the error's endpoint matches the service selected in CC Switch. If the endpoint is correct but rejects the key, check that service's key/login. This tool does not generate or replace credentials.

## Backups and recovery

Backups live under `~/.codex/recovery_backups/<timestamp>-codex-switch/`. Before any mutation, v4 backs up the complete current state: config, all rollout files, and consistent copies of every known database. `restore` merges missing data instead of replacing the current snapshot. If it reports an ambiguous conflict, stop and inspect the two copies rather than forcing an overwrite.

If an old release already produced a compressed rollout, the complete canonical copy may still exist in an earlier recovery backup. Preserve all backups and compare them before attempting repair. Never use an old `restore` to “try again”.

List candidate backups without changing data:

~~~bash
codex-switch backups
~~~

Choose an explicit backup path after inspection; the newest backup may itself be a pre-restore snapshot. First test recovery against a separate copy of your Codex home:

~~~bash
codex-switch --codex-home /path/to/test-home restore /path/to/candidate-backup --no-open
~~~

Review recovered/conflicting files and indexes before applying recovery to real data. A safety backup preserves the pre-recovery state, but it does not make ambiguous histories automatically mergeable.

## Development

~~~bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
~~~

Tests use isolated temporary Codex homes. This project depends on undocumented Codex Desktop storage schemas and is not affiliated with or endorsed by OpenAI.

An optional native routing probe requires Node.js and an installed Codex CLI. It uses temporary data, a dummy key, and a loopback server only; it does not call a real model:

~~~bash
CODEX_BINARY=/path/to/codex node tests/route_probe.mjs
~~~

## License

[MIT](LICENSE)

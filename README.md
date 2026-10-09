# Codex Provider Sync

[简体中文](README.zh-CN.md)

`codex-switch` is an unofficial, recovery-oriented CLI for Codex Desktop on macOS. It changes the active provider without rewriting provider-native conversation history. It works with Codex's undocumented local files, so keep backups and test against a copy when Codex itself has just updated.

> [!CAUTION]
> **Stop using releases <= 0.3.2 for real data.** Those versions could normalize/delete provider-native response items, relabel every thread as one provider, and restore an old database snapshot over newer chats. Keep every existing directory under `~/.codex/recovery_backups/`, upgrade first, and do not run an old `sync` or `restore` while troubleshooting. This project cannot recreate history already removed by an old release; recover from the best untouched backup instead.

## What changed in 0.4.0

- `switch` changes only the root `model_provider` and creates a complete backup of config, databases, and all rollout files. It does **not** rewrite JSONL response items, reasoning, compaction, tool state, thread provenance, or the sidebar index.
- `switch` with no provider argument auto-toggles only when exactly one `openai` provider and one non-OpenAI custom provider are configured. Otherwise pass the provider explicitly.
- `sync` is an index reconciliation command. It reads provider provenance per rollout, repairs only matching database rows, preserves mixed-provider installations, and keeps unmatched catalog rows.
- `restore` is additive: missing rollouts and missing database rows are copied in while current/new data is retained. A known exact-prefix or legacy-normalized snapshot may be repaired with its canonical original plus an unambiguous appended suffix. Ambiguous conflicts are left untouched and reported.
- Restoring a backup changes only the root provider setting; it does not replace the whole current config. Every restore first makes a safety backup containing all current rollouts.
- No backups are pruned by default. `--keep N` only applies when explicitly requested and never deletes legacy/recovery backups.

The history normalizer remains a library boundary for a host that is deliberately serializing a request. The CLI does not invoke it to mutate persisted history, and it is not an automatic request serializer.

## Important limitation

Provider-native response IDs, encrypted reasoning, compaction state, tool-call state, and continuation lineage may only work with the provider/account/endpoint/transport that created them. A provider switch cannot make an old native chat portable or repair lineage that Codex has already rejected. Continue an old chat with its original provider, or start a new chat after switching. Do not treat a clean `sync` report as proof that cross-provider continuation is supported.

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

To upgrade an existing checkout, pull the new release and rerun `./install.sh`. Verify `codex-switch --version` reports 0.4.0 or later before any repair operation.

## Commands

| Command | Purpose |
| --- | --- |
| `codex-switch status [--json]` | Inspect provider provenance and index counts. |
| `codex-switch switch [provider]` | Change the root provider. With no argument, auto-toggle only for exactly one OpenAI + one custom provider. |
| `codex-switch sync` | Reconcile indexes using each rollout's recorded provider; does not rewrite history. |
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

Or choose it directly:

~~~bash
codex-switch switch custom
~~~

OpenAI is built in; other providers must already be declared in `~/.codex/config.toml`. The tool does not create credentials.

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

## License

[MIT](LICENSE)

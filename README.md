# Codex Provider Sync

[简体中文](README.zh-CN.md)

`codex-switch` is a recovery-oriented command-line tool for Codex Desktop on macOS. It keeps local conversation metadata consistent when changing model providers, and repairs history that still exists on disk but no longer appears correctly in the sidebar.

> [!CAUTION]
> This is an unofficial alpha utility that works with Codex's internal local-state files. Codex may change those files without notice. The tool creates rollback backups and validates SQLite databases, but you should still understand the scope below before running it.

## Why this exists

Codex history is represented in several layers:

```text
~/.codex/sessions and archived_sessions
└── JSONL rollout files containing conversation records

~/.codex/state_5.sqlite
~/.codex/sqlite/state_5.sqlite
└── thread metadata and compatibility indexes

~/.codex/sqlite/codex-dev.db
└── the desktop sidebar catalog
```

After a provider change, these layers can disagree about `model_provider`. On affected installations, conversations remain in the rollout files while one database, the compatibility database, or the sidebar catalog still points at the previous provider. The result can look like missing history, stale titles, or newly forked continuation threads.

`codex-switch` inspects and reconciles all of these layers as one operation.

## What it does

- Reports provider counts across rollout files, both known thread databases, and the sidebar catalog.
- Normalizes provider-bound response items, then synchronizes rollout `session_meta` records to the selected provider.
- Merges missing thread IDs between both `state_5.sqlite` locations.
- Reconciles conflicting shared metadata using the newer thread record.
- Repairs user-event visibility flags and rebuilds the local sidebar catalog.
- Quits Codex before writes and reopens it afterward.
- Creates SQLite-consistent backups before every mutation.
- Rolls back automatically if a write or validation step fails.
- Provides explicit backup listing and restore commands.

It does **not** configure provider credentials, translate encrypted reasoning state, or merge separate forked threads into one conversation.

## Responses API history normalization

The metadata repair performed by `codex-switch` is separate from request
serialization. The reusable `codex_provider_sync.history` module is the
normalization boundary for code that turns persisted response items into a
provider request:

```python
from codex_provider_sync.history import normalize_response_history

normalized = normalize_response_history(
    persisted_items,
    source_provider=session_source_provider,
    target_provider=active_provider,
    source_endpoint=session_source_endpoint,
    source_model=session_source_model,
    source_auth_mode=session_source_auth_mode,
    source_transport=session_source_transport,
    source_continuation_scope=session_continuation_scope,
    target_endpoint=active_endpoint,
    target_model=active_model,
    target_auth_mode=active_auth_mode,
    target_transport=active_transport,
    target_continuation_scope=active_continuation_scope,
    continuation_compatible=same_authenticated_continuation_domain,
)
request_input = normalized.items
```

The normalizer keeps provenance out of the API payload. When providers differ,
it preserves ordinary message content, removes provider-generated IDs, drops
opaque reasoning/compaction state (retaining a visible summary when one is
available), and removes tool-call state as a complete unit. It never rewrites
an ID prefix such as `item_` to `rs_`. It preserves native state only when the
request host explicitly confirms that the source and target share the same
authenticated continuation domain; a matching provider name or ID prefix is
not enough. Preserved state is still checked for valid OpenAI item namespaces
and tool-call relationships. Missing provenance is treated conservatively.

`codex-switch` also applies this normalization when it migrates rollout files.
The reusable function should still be called by any other host that loads raw
persisted items immediately before it builds the Responses API `input` array.

## Requirements

- macOS
- Codex Desktop using the default local state layout under `~/.codex`
- Python 3.10 or newer
- A target provider already configured in `~/.codex/config.toml`

## Install

```bash
git clone git@github.com:ZPXing-TAA/codex-provider-sync.git
cd codex-provider-sync
./install.sh
```

The installer creates an isolated virtual environment under `~/.local/share/codex-provider-sync` and installs a small shell launcher at `~/.local/bin/codex-switch`.

Make sure `~/.local/bin` is in `PATH`. Then verify:

```bash
codex-switch --version
codex-switch status
```

To remove the command while keeping recovery backups:

```bash
./uninstall.sh
```

## Quick start

First, inspect the current state:

```bash
codex-switch status
```

If `config.toml` already selects the provider you want, synchronize history to it:

```bash
codex-switch sync
```

The command will:

1. warn before normalizing histories that contain provider-native response items;
2. quit Codex;
3. create a rollback backup;
4. reconcile rollout metadata, databases, and the sidebar catalog;
5. validate the resulting databases;
6. reopen Codex.

For a provider already declared under `[model_providers.<id>]`, you can update the root `model_provider` and synchronize in one operation:

```bash
codex-switch switch custom
```

`switch` refuses providers that are not already configured. It never creates credentials or guesses endpoint settings.

## Commands

| Command | Purpose |
| --- | --- |
| `codex-switch status` | Show provider counts across all known history layers. |
| `codex-switch status --json` | Emit machine-readable status. |
| `codex-switch sync` | Synchronize history to the provider currently selected in `config.toml`. |
| `codex-switch switch <provider>` | Select an already-configured provider and synchronize history. |
| `codex-switch backups` | List rollback backups, newest first. |
| `codex-switch restore [path]` | Restore a backup; defaults to the newest backup. |

Useful options:

- `--yes`: accept provider-state normalization non-interactively.
- `--keep N`: retain the newest `N` rollback backups. The default is 5.
- `--no-open`: do not reopen Codex after the operation.
- `--codex-home PATH`: inspect or operate on another Codex home. Place this global option before the command.

Example:

```bash
codex-switch --codex-home /path/to/test-home status --json
```

## Reading status output

Example:

```json
{
  "config_provider": "openai",
  "rollouts": {"openai": 214},
  "response_item_rollouts": {"openai": 203},
  "response_items": {"openai": 15482},
  "databases": {
    "state_5.sqlite": {"openai": 212},
    "sqlite/state_5.sqlite": {"openai": 164}
  },
  "catalog": {"openai": 139}
}
```

Different totals do not automatically mean data loss:

- Rollouts include active, archived, empty, and internal sessions.
- Thread databases index interactive and internal thread metadata.
- The sidebar catalog includes only unarchived conversations with user-visible content.

The important signal is provider consistency within each layer. `sync` also makes both thread databases contain the same known thread IDs.

## Backups and recovery

Backups are stored under:

```text
~/.codex/recovery_backups/<timestamp>-codex-switch/
```

Each backup contains:

- `config.toml` when present;
- SQLite-consistent copies of the known databases;
- only the rollout files that the operation will modify;
- a manifest describing the actual rollout source-provider counts, target provider, and backed-up paths.

List backups:

```bash
codex-switch backups
```

Restore the newest backup:

```bash
codex-switch restore
```

Restore a specific backup:

```bash
codex-switch restore ~/.codex/recovery_backups/<timestamp>-codex-switch
```

Before restoring, the tool creates another safety backup of the current state, so a restore can itself be undone.

## Provider-bound history

Response IDs, encrypted reasoning, compaction state, and tool-call continuation state may be valid only for the provider, account, endpoint, transport, or live connection that produced them. Replaying them after a switch can cause errors such as `invalid_id_prefix` or `persisted-item lookup ... not supported`.

For each rollout that actually changes provider, version 0.3.1:

- keeps ordinary user, assistant, system, and developer message content;
- strips provider-generated IDs from portable messages;
- converts a visible reasoning summary to an ordinary assistant message;
- removes opaque reasoning, compaction, unknown provider state, and tool-call pairs;
- records the untouched original in the rollback backup first.

This trades provider-native continuation efficiency and historical tool traces for portable conversational context. `--yes` accepts that normalization; it never makes opaque state portable.

## Files modified

Depending on which files exist, synchronization may update:

```text
~/.codex/config.toml                 # switch only
~/.codex/sessions/**/*.jsonl         # session_meta + cross-provider response-item normalization
~/.codex/archived_sessions/*.jsonl
~/.codex/state_5.sqlite
~/.codex/sqlite/state_5.sqlite
~/.codex/sqlite/codex-dev.db
```

Writes are protected by a process lock, atomic rollout/config replacement, SQLite transactions, pre-write backups, and post-write `PRAGMA quick_check` validation.

## Troubleshooting

### `zsh: killed codex-switch ...`

Re-run `./install.sh`. The installer uses a shell launcher and keeps the Python module in an isolated virtual environment. This avoids a macOS execution-policy issue observed with directly executed Python scripts carrying local provenance metadata.

### `provider 'custom' is not configured`

Define the provider in `~/.codex/config.toml` first. The tool intentionally does not invent provider settings or credentials.

### `confirmation required`

The command detected provider-mismatched rollout files containing provider-native response items while running non-interactively. Review the normalization rules above, then rerun with `--yes`.

### Codex did not reopen

The data operation may still have succeeded. Check the terminal output and run:

```bash
open -a Codex
codex-switch status
```

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

The test suite uses isolated temporary Codex homes and covers provider validation, TOML root-key handling, provider-state confirmation and migration, database reconciliation, idempotency, automatic rollback, App reopening, explicit restore, and provider-aware Responses API history normalization.

## Project status

This project targets a real recovery problem but relies on undocumented Codex Desktop storage schemas. Treat releases as schema-specific and test against a copy of your Codex home when using a newer Codex build for the first time.

This project is not affiliated with or endorsed by OpenAI.

## License

[MIT](LICENSE)

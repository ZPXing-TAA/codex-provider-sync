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
- Synchronizes rollout `session_meta` records to the provider already selected in `config.toml`.
- Merges missing thread IDs between both `state_5.sqlite` locations.
- Reconciles conflicting shared metadata using the newer thread record.
- Repairs user-event visibility flags and rebuilds the local sidebar catalog.
- Quits Codex before writes and reopens it afterward.
- Creates SQLite-consistent backups before every mutation.
- Rolls back automatically if a write or validation step fails.
- Provides explicit backup listing and restore commands.

It does **not** configure provider credentials, decrypt provider-bound history, or merge separate forked threads into one conversation.

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

1. warn before relabeling histories that contain `encrypted_content`;
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

- `--yes`: acknowledge the encrypted-history compatibility warning non-interactively.
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
- a manifest describing the source provider, target provider, and backed-up paths.

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

## Encrypted history limitation

Some rollout files contain `encrypted_content` created by a specific provider or account. Changing `session_meta.model_provider` can make the history visible under the selected provider, but it does not transform or decrypt that encrypted payload.

Consequences:

- a restored conversation may be visible but fail when continued;
- compaction may fail with an encrypted-content validation error;
- returning to the original provider/account may still be required.

The confirmation prompt exists to make this distinction explicit. `--yes` acknowledges the risk; it does not remove it.

## Files modified

Depending on which files exist, synchronization may update:

```text
~/.codex/config.toml                 # switch only
~/.codex/sessions/**/*.jsonl         # first session_meta record only
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

The command detected provider-mismatched rollout files containing encrypted history while running non-interactively. Review the limitation above, then rerun with `--yes` only if the visibility repair is what you want.

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

The test suite uses isolated temporary Codex homes and covers provider validation, TOML root-key handling, encrypted-history confirmation, database reconciliation, idempotency, automatic rollback, App reopening, and explicit restore.

## Project status

This project targets a real recovery problem but relies on undocumented Codex Desktop storage schemas. Treat releases as schema-specific and test against a copy of your Codex home when using a newer Codex build for the first time.

This project is not affiliated with or endorsed by OpenAI.

## License

[MIT](LICENSE)

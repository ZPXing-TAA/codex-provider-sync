import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from codex_provider_sync import cli as MODULE


class CodexSwitchTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        (self.home / "sqlite").mkdir()
        (self.home / "sessions/2026/01/01").mkdir(parents=True)
        (self.home / "archived_sessions").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "codex_provider_sync", "--codex-home", str(self.home), *arguments],
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(SRC)},
        )

    def write_config(self, provider="custom", include_custom=True, extra=""):
        text = f'model_provider = "{provider}"\nmodel = "keep-me"\n'
        if include_custom:
            text += '\n[model_providers.custom]\nname = "Custom"\n'
        text += extra
        (self.home / "config.toml").write_text(text)

    def write_rollout(self, provider="custom", encrypted=False, response_items=()):
        rollout = self.home / "sessions/2026/01/01/rollout.jsonl"
        lines = [
            {"type": "session_meta", "payload": {"id": "t1", "model_provider": provider}},
            {"type": "event_msg", "payload": {"message": "kept"}},
        ]
        if encrypted:
            lines.append({
                "type": "response_item",
                "payload": {"type": "reasoning", "id": "item_old", "encrypted_content": "opaque"},
            })
        lines.extend({"type": "response_item", "payload": item} for item in response_items)
        rollout.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return rollout

    def test_switch_merges_reconciles_and_rebuilds_catalog(self):
        self.write_config()
        rollout = self.write_rollout()
        self.create_state(self.home / "state_5.sqlite", "t1", "custom", title="older", updated=2)
        self.create_state(self.home / "sqlite/state_5.sqlite", "t1", "custom", title="newer", updated=3)
        self.insert_thread(self.home / "sqlite/state_5.sqlite", "t2", "custom", title="second", updated=4)
        self.create_catalog(self.home / "sqlite/codex-dev.db")

        result = self.run_cli("switch", "openai", "--no-quit", "--no-open")

        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.home / "config.toml").read_text()
        self.assertIn('model_provider = "openai"', config)
        self.assertIn('model = "keep-me"', config)
        self.assertEqual(json.loads(rollout.read_text().splitlines()[0])["payload"]["model_provider"], "openai")
        for db_path in (self.home / "state_5.sqlite", self.home / "sqlite/state_5.sqlite"):
            with sqlite3.connect(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM threads WHERE model_provider='openai'").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT title FROM threads WHERE id='t1'").fetchone()[0], "newer")
        with sqlite3.connect(self.home / "sqlite/codex-dev.db") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM local_thread_catalog").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM local_thread_catalog WHERE model_provider='openai'").fetchone()[0], 2)

    def test_sync_rejects_target_different_from_config(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom")
        before = rollout.read_bytes()

        result = self.run_cli("sync", "openai", "--no-quit", "--no-open")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sync cannot target", result.stderr)
        self.assertEqual(rollout.read_bytes(), before)
        self.assertIn('model_provider = "custom"', (self.home / "config.toml").read_text())

    def test_switch_rejects_unconfigured_provider(self):
        self.write_config("openai", include_custom=False)
        self.write_rollout("openai")

        result = self.run_cli("switch", "custom", "--no-quit", "--no-open")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not configured", result.stderr)
        self.assertIn('model_provider = "openai"', (self.home / "config.toml").read_text())

    def test_sync_rejects_current_provider_without_configuration(self):
        self.write_config("custom", include_custom=False)
        self.write_rollout("custom")

        result = self.run_cli("sync", "--no-quit", "--no-open")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("that provider is not configured", result.stderr)

    def test_nested_provider_is_not_mistaken_for_root_provider(self):
        (self.home / "config.toml").write_text(
            'model = "keep-me"\n\n[profiles.demo]\nmodel_provider = "custom"\n'
        )
        self.write_rollout("openai")

        result = self.run_cli("status", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["config_provider"], "openai")
        self.assertEqual(data["configured_providers"], ["openai"])

    def test_switch_handles_config_without_trailing_newline(self):
        (self.home / "config.toml").write_text('model = "keep-me"')
        self.write_rollout("openai")

        result = self.run_cli("switch", "openai", "--no-quit", "--no-open")

        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.run_cli("status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["config_provider"], "openai")
        self.assertIn('model = "keep-me"\nmodel_provider = "openai"', (self.home / "config.toml").read_text())

    def test_sync_auto_normalizes_provider_state_without_confirmation(self):
        self.write_config("openai")
        rollout = self.write_rollout("custom", encrypted=True)

        result = self.run_cli("sync", "--no-quit", "--no-open")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Provider-state normalization", result.stderr)
        self.assertNotIn("encrypted_content", rollout.read_text())
        self.assertEqual(
            json.loads(rollout.read_text().splitlines()[0])["payload"]["model_provider"],
            "openai",
        )

    def test_switch_sanitizes_provider_native_response_state(self):
        self.write_config("custom")
        rollout = self.write_rollout(
            "custom",
            response_items=(
                {
                    "type": "message",
                    "role": "user",
                    "id": "msg_custom_user",
                    "content": [{"type": "input_text", "text": "question"}],
                },
                {
                    "type": "reasoning",
                    "id": "rs_looks_valid_but_custom",
                    "encrypted_content": "opaque",
                    "summary": [{"text": "portable summary"}],
                },
                {
                    "type": "function_call",
                    "id": "fc_custom",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "result"},
                {
                    "type": "message",
                    "role": "assistant",
                    "id": "msg_custom_assistant",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
            ),
        )

        result = self.run_cli("switch", "openai", "--no-quit", "--no-open")

        self.assertEqual(result.returncode, 0, result.stderr)
        response_items = [
            record["payload"]
            for record in map(json.loads, rollout.read_text().splitlines())
            if record["type"] == "response_item"
        ]
        self.assertEqual([item["type"] for item in response_items], ["message", "message", "message"])
        self.assertEqual(response_items[1]["content"][0]["text"], "portable summary")
        self.assertTrue(all("id" not in item for item in response_items))
        self.assertNotIn("opaque", rollout.read_text())
        self.assertIn("Provider-native response items removed: 2", result.stdout)
        self.assertIn("Reasoning summaries converted to messages: 1", result.stdout)
        self.assertIn("Provider-generated IDs stripped: 2", result.stdout)

    def test_backup_records_rollout_source_not_already_changed_config(self):
        self.write_config("openai")
        self.write_rollout("custom")

        result = self.run_cli("sync", "--no-quit", "--no-open")

        self.assertEqual(result.returncode, 0, result.stderr)
        backup_line = next(line for line in result.stdout.splitlines() if line.startswith("Backup: "))
        manifest = json.loads(
            (Path(backup_line.removeprefix("Backup: ")) / "manifest.json").read_text()
        )
        self.assertEqual(manifest["source_provider"], "custom")
        self.assertEqual(manifest["source_provider_counts"], {"custom": 1})

    def test_failure_after_mutation_rolls_everything_back(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom")
        self.create_state(self.home / "state_5.sqlite", "t1", "custom")
        self.create_state(self.home / "sqlite/state_5.sqlite", "t1", "custom")
        with sqlite3.connect(self.home / "sqlite/codex-dev.db") as db:
            db.execute("CREATE TABLE local_thread_catalog (host_id TEXT, thread_id TEXT)")
        config_before = (self.home / "config.toml").read_bytes()
        rollout_before = rollout.read_bytes()

        result = self.run_cli("switch", "openai", "--no-quit", "--no-open")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rolled back", result.stderr)
        self.assertEqual((self.home / "config.toml").read_bytes(), config_before)
        self.assertEqual(rollout.read_bytes(), rollout_before)
        for db_path in (self.home / "state_5.sqlite", self.home / "sqlite/state_5.sqlite"):
            with sqlite3.connect(db_path) as db:
                self.assertEqual(db.execute("SELECT DISTINCT model_provider FROM threads").fetchall(), [("custom",)])

    def test_failure_reopens_codex_when_tool_started_lifecycle(self):
        self.write_config("custom")
        self.write_rollout("custom")
        self.create_state(self.home / "state_5.sqlite", "t1", "custom")
        self.create_state(self.home / "sqlite/state_5.sqlite", "t1", "custom")
        with sqlite3.connect(self.home / "sqlite/codex-dev.db") as db:
            db.execute("CREATE TABLE local_thread_catalog (host_id TEXT, thread_id TEXT)")
        args = argparse.Namespace(
            codex_home=str(self.home), command="switch", provider="openai",
            no_quit=False, no_open=False, keep=5,
        )

        with patch.object(MODULE, "quit_codex") as quit_mock, patch.object(MODULE, "open_codex") as open_mock:
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                MODULE.run_sync(args)

        quit_mock.assert_called_once_with()
        open_mock.assert_called_once_with()

    def test_restore_recovers_pre_switch_state_and_makes_safety_backup(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom")
        self.create_state(self.home / "state_5.sqlite", "t1", "custom")
        self.create_state(self.home / "sqlite/state_5.sqlite", "t1", "custom")
        self.create_catalog(self.home / "sqlite/codex-dev.db")
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup_line = next(line for line in switched.stdout.splitlines() if line.startswith("Backup: "))
        backup = backup_line.removeprefix("Backup: ")

        restored = self.run_cli("restore", backup, "--no-quit", "--no-open")

        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn("Safety backup", restored.stdout)
        self.assertIn('model_provider = "custom"', (self.home / "config.toml").read_text())
        self.assertEqual(json.loads(rollout.read_text().splitlines()[0])["payload"]["model_provider"], "custom")
        with sqlite3.connect(self.home / "state_5.sqlite") as db:
            self.assertEqual(db.execute("SELECT DISTINCT model_provider FROM threads").fetchall(), [("custom",)])

    def test_sync_is_idempotent_and_supports_one_state_database(self):
        self.write_config("openai", include_custom=False)
        self.write_rollout("openai")
        self.create_state(self.home / "state_5.sqlite", "t1", "openai")
        self.create_catalog(self.home / "sqlite/codex-dev.db")

        first = self.run_cli("sync", "--no-quit", "--no-open")
        second = self.run_cli("sync", "--no-quit", "--no-open")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Rollout files updated: 0", second.stdout)
        self.assertIn("SQLite rows reconciled: 0", second.stdout)
        self.assertIn("SQLite provider rows updated: 0", second.stdout)

    @staticmethod
    def create_state(path, thread_id, provider, title="Title", updated=2):
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE threads (
                  id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL,
                  cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
                  approval_mode TEXT NOT NULL, has_user_event INTEGER NOT NULL DEFAULT 0,
                  archived INTEGER NOT NULL DEFAULT 0, first_user_message TEXT NOT NULL DEFAULT '',
                  preview TEXT NOT NULL DEFAULT '', created_at_ms INTEGER, updated_at_ms INTEGER,
                  git_branch TEXT, thread_source TEXT
                );
            """)
        CodexSwitchTest.insert_thread(path, thread_id, provider, title=title, updated=updated)

    @staticmethod
    def insert_thread(path, thread_id, provider, title="Title", updated=2):
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                thread_id, f"/{thread_id}.jsonl", 1, updated, "app", provider, "/tmp/project",
                title, "{}", "never", 1, 0, "hello", "hello", 1000,
                updated * 1000, None, None,
            ))

    @staticmethod
    def create_catalog(path):
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE local_thread_catalog (
                  host_id TEXT NOT NULL, thread_id TEXT NOT NULL, display_title TEXT NOT NULL,
                  source_created_at REAL NOT NULL, source_updated_at REAL NOT NULL, cwd TEXT NOT NULL,
                  source_kind TEXT NOT NULL, source_detail TEXT, model_provider TEXT NOT NULL,
                  git_branch TEXT, observation_sequence INTEGER NOT NULL,
                  missing_candidate INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(host_id,thread_id));
                CREATE TABLE local_thread_catalog_sync_state (
                  host_id TEXT PRIMARY KEY, watermark_updated_at REAL,
                  initial_build_complete INTEGER NOT NULL DEFAULT 0,
                  observation_sequence INTEGER NOT NULL DEFAULT 0);
            """)


if __name__ == "__main__":
    unittest.main()

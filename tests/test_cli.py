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

    def write_rollout(self, provider="custom", encrypted=False, response_items=(), thread_id="t1"):
        filename = "rollout.jsonl" if thread_id == "t1" else f"rollout-{thread_id}.jsonl"
        rollout = self.home / "sessions/2026/01/01" / filename
        lines = [
            {"type": "session_meta", "payload": {"id": thread_id, "model_provider": provider}},
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

    def test_switch_changes_only_config_and_backs_up_all_history(self):
        self.write_config()
        rollout = self.write_rollout()
        other_rollout = self.write_rollout("openai", encrypted=True, thread_id="t2")
        self.create_state(self.home / "state_5.sqlite", "t1", "custom", title="older", updated=2)
        self.create_state(self.home / "sqlite/state_5.sqlite", "t1", "custom", title="newer", updated=3)
        self.insert_thread(self.home / "sqlite/state_5.sqlite", "t2", "custom", title="second", updated=4)
        self.create_catalog(self.home / "sqlite/codex-dev.db")
        self.insert_catalog("t1", "custom")
        untouched = {path: path.read_bytes() for path in (
            rollout, other_rollout, self.home / "state_5.sqlite",
            self.home / "sqlite/state_5.sqlite", self.home / "sqlite/codex-dev.db",
        )}

        result = self.run_cli("switch", "openai", "--no-quit", "--no-open")

        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.home / "config.toml").read_text()
        self.assertIn('model_provider = "openai"', config)
        self.assertIn('model = "keep-me"', config)
        for path, original in untouched.items():
            self.assertEqual(path.read_bytes(), original, str(path))
        backup = self.result_backup(result)
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(set(manifest["rollouts"]), {
            str(path.relative_to(self.home)) for path in (rollout, other_rollout)
        })
        for path in (rollout, other_rollout):
            self.assertEqual((backup / "rollouts" / path.relative_to(self.home)).read_bytes(), untouched[path])

    def test_switch_without_provider_toggles_both_directions_without_yes(self):
        self.write_config("openai")
        rollout = self.write_rollout("openai", encrypted=True)
        before = rollout.read_bytes()

        first = self.run_cli("switch", "--no-quit", "--no-open")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('model_provider = "custom"', (self.home / "config.toml").read_text())
        second = self.run_cli("switch", "--no-quit", "--no-open")

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('model_provider = "openai"', (self.home / "config.toml").read_text())
        self.assertEqual(rollout.read_bytes(), before)

    def test_switch_without_provider_rejects_ambiguous_config(self):
        self.write_config("openai", extra='\n[model_providers.other]\nname = "Other"\n')
        before = (self.home / "config.toml").read_bytes()

        result = self.run_cli("switch", "--no-quit", "--no-open")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.home / "config.toml").read_bytes(), before)

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

    def test_switch_preserves_provider_native_history_byte_for_byte(self):
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
        before = rollout.read_bytes()
        response_count = sum(
            json.loads(line).get("type") == "response_item"
            for line in before.splitlines()
        )

        result = self.run_cli("switch", "openai", "--no-quit", "--no-open")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rollout.read_bytes(), before)
        self.assertEqual(
            sum(json.loads(line).get("type") == "response_item" for line in rollout.read_bytes().splitlines()),
            response_count,
        )
        self.assertIn(b'"encrypted_content": "opaque"', rollout.read_bytes())

    def test_sync_preserves_rollouts_and_reconciles_only_matched_provider_rows(self):
        self.write_config("openai")
        custom_rollout = self.write_rollout("custom", encrypted=True, thread_id="t1")
        openai_rollout = self.write_rollout("openai", encrypted=True, thread_id="t2")
        custom_before, openai_before = custom_rollout.read_bytes(), openai_rollout.read_bytes()
        state = self.home / "state_5.sqlite"
        self.create_state(state, "t1", "openai")
        self.set_thread_rollout(state, "t1", custom_rollout)
        self.insert_thread(state, "t2", "custom")
        self.set_thread_rollout(state, "t2", openai_rollout)
        self.insert_thread(state, "unmatched", "custom")
        self.create_catalog(self.home / "sqlite/codex-dev.db")
        for thread_id, provider in (("t1", "openai"), ("t2", "custom"), ("unmatched", "custom")):
            self.insert_catalog(thread_id, provider)

        result = self.run_cli("sync", "--no-quit", "--no-open")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(custom_rollout.read_bytes(), custom_before)
        self.assertEqual(openai_rollout.read_bytes(), openai_before)
        with sqlite3.connect(state) as db:
            self.assertEqual(db.execute("SELECT model_provider FROM threads WHERE id='t1'").fetchone()[0], "custom")
            self.assertEqual(db.execute("SELECT model_provider FROM threads WHERE id='t2'").fetchone()[0], "openai")
            self.assertEqual(db.execute("SELECT model_provider FROM threads WHERE id='unmatched'").fetchone()[0], "custom")
        with sqlite3.connect(self.home / "sqlite/codex-dev.db") as db:
            providers = dict(db.execute("SELECT thread_id,model_provider FROM local_thread_catalog"))
        self.assertEqual(providers["unmatched"], "custom")
        self.assertEqual(providers["t1"], "custom")
        self.assertEqual(providers["t2"], "openai")

    def test_backup_contains_rollouts_from_mixed_provider_history(self):
        self.write_config("openai")
        self.write_rollout("custom", thread_id="t1")
        self.write_rollout("openai", thread_id="t2")

        result = self.run_cli("sync", "--no-quit", "--no-open")

        self.assertEqual(result.returncode, 0, result.stderr)
        backup = self.result_backup(result)
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(set(manifest["rollouts"]), {
            "sessions/2026/01/01/rollout.jsonl",
            "sessions/2026/01/01/rollout-t2.jsonl",
        })
        self.assertEqual(
            json.loads((backup / "rollouts/sessions/2026/01/01/rollout.jsonl").read_text().splitlines()[0])[
                "payload"]["model_provider"],
            "custom",
        )
        self.assertEqual(
            json.loads((backup / "rollouts/sessions/2026/01/01/rollout-t2.jsonl").read_text().splitlines()[0])[
                "payload"]["model_provider"],
            "openai",
        )

    def test_failure_after_config_mutation_rolls_back_without_touching_history(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom", encrypted=True)
        self.create_state(self.home / "state_5.sqlite", "t1", "custom")
        self.create_catalog(self.home / "sqlite/codex-dev.db")
        self.insert_catalog("t1", "custom")
        config_before = (self.home / "config.toml").read_bytes()
        rollout_before = rollout.read_bytes()
        db_before = self.sqlite_snapshot(self.home / "state_5.sqlite")
        catalog_before = self.sqlite_snapshot(self.home / "sqlite/codex-dev.db")
        args = MODULE.build_parser().parse_args([
            "--codex-home", str(self.home), "switch", "openai", "--no-quit", "--no-open"
        ])
        original_set_config_provider = MODULE.set_config_provider

        calls = 0

        def mutate_then_fail_once(*call_args, **call_kwargs):
            nonlocal calls
            calls += 1
            original_set_config_provider(*call_args, **call_kwargs)
            if calls == 1:
                raise RuntimeError("injected failure after config mutation")

        with patch.object(MODULE, "set_config_provider", side_effect=mutate_then_fail_once):
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                MODULE.run_sync(args)

        self.assertEqual((self.home / "config.toml").read_bytes(), config_before)
        self.assertEqual(rollout.read_bytes(), rollout_before)
        self.assertEqual(self.sqlite_snapshot(self.home / "state_5.sqlite"), db_before)
        self.assertEqual(self.sqlite_snapshot(self.home / "sqlite/codex-dev.db"), catalog_before)

    def test_failure_reopens_codex_when_tool_started_lifecycle(self):
        self.write_config("custom")
        self.write_rollout("custom")
        args = MODULE.build_parser().parse_args([
            "--codex-home", str(self.home), "switch", "openai"
        ])

        fail_once = True

        def fail_config_mutation(*_args, **_kwargs):
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise RuntimeError("injected failure")

        with (
            patch.object(MODULE, "quit_codex") as quit_mock,
            patch.object(MODULE, "open_codex") as open_mock,
            patch.object(MODULE, "set_config_provider", side_effect=fail_config_mutation),
        ):
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                MODULE.run_sync(args)

        quit_mock.assert_called_once_with()
        open_mock.assert_called_once_with()

    def test_restore_recovers_pre_switch_state_and_makes_safety_backup(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom")
        self.create_state(self.home / "state_5.sqlite", "t1", "custom")
        self.set_thread_rollout(self.home / "state_5.sqlite", "t1", rollout)
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

    def test_restore_preserves_appended_history_and_new_files_rows_and_catalog(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom", encrypted=True)
        missing_rollout = self.write_rollout("custom", encrypted=True, thread_id="missing")
        original = rollout.read_bytes()
        missing_original = missing_rollout.read_bytes()
        state = self.home / "state_5.sqlite"
        self.create_state(state, "t1", "custom")
        self.set_thread_rollout(state, "t1", rollout)
        self.insert_thread(state, "missing", "custom")
        self.set_thread_rollout(state, "missing", missing_rollout)
        self.create_catalog(self.home / "sqlite/codex-dev.db")
        self.insert_catalog("t1", "custom")
        self.insert_catalog("missing", "custom")
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        suffix = json.dumps({"type": "event_msg", "payload": {"message": "new custom work"}}).encode() + b"\n"
        rollout.write_bytes(original + suffix)
        new_rollout = self.write_rollout("openai", encrypted=True, thread_id="new")
        new_bytes = new_rollout.read_bytes()
        self.insert_thread(state, "new", "openai", title="new thread", updated=10)
        self.set_thread_rollout(state, "new", new_rollout)
        self.insert_catalog("new", "openai", title="new thread")
        self.insert_catalog("catalog-only", "custom", title="keep unmatched")
        with sqlite3.connect(state) as db:
            db.execute("UPDATE threads SET title='newer title',updated_at=11,updated_at_ms=11000,model_provider='openai' WHERE id='t1'")
            db.execute("DELETE FROM threads WHERE id='missing'")
        with sqlite3.connect(self.home / "sqlite/codex-dev.db") as db:
            db.execute("DELETE FROM local_thread_catalog WHERE thread_id='missing'")
        missing_rollout.unlink()

        restored = self.run_cli("restore", str(backup), "--no-quit", "--no-open")

        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn('model_provider = "custom"', (self.home / "config.toml").read_text())
        self.assertEqual(rollout.read_bytes(), original + suffix)
        self.assertEqual(new_rollout.read_bytes(), new_bytes)
        self.assertEqual(missing_rollout.read_bytes(), missing_original)
        with sqlite3.connect(state) as db:
            rows = dict(db.execute("SELECT id,model_provider FROM threads"))
            self.assertEqual(rows, {"t1": "custom", "new": "openai", "missing": "custom"})
            self.assertEqual(db.execute("SELECT title FROM threads WHERE id='t1'").fetchone()[0], "newer title")
        with sqlite3.connect(self.home / "sqlite/codex-dev.db") as db:
            catalog = dict(db.execute("SELECT thread_id,model_provider FROM local_thread_catalog"))
            self.assertEqual(catalog["new"], "openai")
            self.assertEqual(catalog["catalog-only"], "custom")
            self.assertEqual(catalog["missing"], "custom")
        safety = next(
            path for path in MODULE.backup_paths(self.home)
            if json.loads((path / "manifest.json").read_text())["operation"] == "pre-restore"
        )
        manifest = json.loads((safety / "manifest.json").read_text())
        self.assertIn(str(new_rollout.relative_to(self.home)), manifest["rollouts"])
        self.assertEqual((safety / "rollouts" / new_rollout.relative_to(self.home)).read_bytes(), new_bytes)

    def test_restore_preserves_conflicting_current_history(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom", encrypted=True)
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        current = rollout.read_bytes().replace(b"kept", b"legitimately edited current history")
        rollout.write_bytes(current)

        restored = self.run_cli("restore", str(backup), "--no-quit", "--no-open")

        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(rollout.read_bytes(), current)

    def test_restore_recovers_old_metadata_relabel_and_retains_suffix(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom", encrypted=True)
        original = rollout.read_bytes()
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(version=3, operation="switch", target_provider="openai")
        manifest_path.write_text(json.dumps(manifest) + "\n")
        records = original.splitlines(keepends=True)
        session_meta = json.loads(records[0])
        session_meta["payload"]["model_provider"] = "openai"
        suffix = json.dumps({"type": "event_msg", "payload": {"message": "new appended turn"}}).encode() + b"\n"
        rollout.write_bytes(json.dumps(session_meta).encode() + b"\n" + b"".join(records[1:]) + suffix)

        restored = self.run_cli("restore", str(backup), "--no-quit", "--no-open")

        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(rollout.read_bytes(), original + suffix)

    def test_restore_recovers_old_lossy_normalizer_without_losing_new_native_items(self):
        self.write_config("custom")
        message = {
            "type": "message", "role": "assistant", "id": "msg_old",
            "content": [{"type": "output_text", "text": "original answer"}],
        }
        rollout = self.write_rollout("custom", encrypted=True, response_items=(
            message,
            {"type": "function_call", "id": "fc_old", "call_id": "call_old", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_old", "output": "tool answer"},
        ))
        original = rollout.read_bytes()
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(version=3, operation="switch", target_provider="openai")
        manifest_path.write_text(json.dumps(manifest) + "\n")
        portable_message = {key: value for key, value in message.items() if key != "id"}
        suffix = json.dumps({
            "type": "response_item", "payload": {
                "type": "reasoning", "id": "rs_new", "encrypted_content": "new opaque reasoning",
            },
        }).encode() + b"\n"
        for live_provider in ("openai", "custom"):
            with self.subTest(live_provider=live_provider):
                records = list(map(json.loads, original.splitlines()))
                records[0]["payload"]["model_provider"] = live_provider
                normalized = [records[0], records[1], {"type": "response_item", "payload": portable_message}]
                rollout.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in normalized) + suffix)

                restored = self.run_cli("restore", str(backup), "--no-quit", "--no-open")

                self.assertEqual(restored.returncode, 0, restored.stderr)
                self.assertEqual(rollout.read_bytes(), original + suffix)

    def test_restore_preserves_same_provider_opaque_tail_edit_without_legacy_evidence(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom", encrypted=True)
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        current = rollout.read_bytes().replace(b'"encrypted_content": "opaque"', b'"encrypted_content": "legitimate edited opaque state"')
        rollout.write_bytes(current)

        restored = self.run_cli("restore", str(backup), "--no-quit", "--no-open")

        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(rollout.read_bytes(), current)
        self.assertIn("Conflict left unchanged", restored.stderr)

    def test_restore_failure_rolls_back_to_complete_pre_restore_state(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom", encrypted=True)
        missing_rollout = self.write_rollout("custom", encrypted=True, thread_id="missing")
        state = self.home / "state_5.sqlite"
        self.create_state(state, "t1", "custom")
        self.set_thread_rollout(state, "t1", rollout)
        self.insert_thread(state, "missing", "custom")
        self.set_thread_rollout(state, "missing", missing_rollout)
        self.create_catalog(self.home / "sqlite/codex-dev.db")
        self.insert_catalog("t1", "custom")
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        missing_rollout.unlink()
        with sqlite3.connect(state) as db:
            db.execute("DELETE FROM threads WHERE id='missing'")
        new_rollout = self.write_rollout("openai", encrypted=True, thread_id="new")
        self.insert_thread(state, "new", "openai")
        self.set_thread_rollout(state, "new", new_rollout)
        self.insert_catalog("new", "openai")
        before = {path: path.read_bytes() for path in (
            self.home / "config.toml", rollout, new_rollout,
        )}
        db_before = self.sqlite_snapshot(state)
        catalog_before = self.sqlite_snapshot(self.home / "sqlite/codex-dev.db")
        args = MODULE.build_parser().parse_args([
            "--codex-home", str(self.home), "restore", str(backup), "--no-quit", "--no-open"
        ])
        original_restore = MODULE.restore_backup
        calls = 0

        def restore_then_fail_once(*call_args, **call_kwargs):
            nonlocal calls
            calls += 1
            result = original_restore(*call_args, **call_kwargs)
            if calls == 1:
                raise RuntimeError("injected failure after recovery")
            return result

        with patch.object(MODULE, "restore_backup", side_effect=restore_then_fail_once):
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                MODULE.run_restore(args)

        for path, original in before.items():
            self.assertEqual(path.read_bytes(), original, str(path))
        self.assertEqual(self.sqlite_snapshot(state), db_before)
        self.assertEqual(self.sqlite_snapshot(self.home / "sqlite/codex-dev.db"), catalog_before)
        self.assertFalse(missing_rollout.exists())

    def test_restore_does_not_import_unmatched_backup_rows_into_existing_databases(self):
        self.assert_restore_skips_unmatched_backup_rows(missing_destination=False)

    def test_restore_does_not_import_unmatched_backup_rows_into_missing_databases(self):
        self.assert_restore_skips_unmatched_backup_rows(missing_destination=True)

    def test_restore_new_required_sqlite_column_fails_and_rolls_back_all_changes(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom", encrypted=True)
        missing_rollout = self.write_rollout("custom", encrypted=True, thread_id="missing")
        state = self.home / "state_5.sqlite"
        self.create_state(state, "t1", "custom")
        self.set_thread_rollout(state, "t1", rollout)
        self.insert_thread(state, "missing", "custom")
        self.set_thread_rollout(state, "missing", missing_rollout)
        self.create_catalog(self.home / "sqlite/codex-dev.db")
        self.insert_catalog("t1", "custom")
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        with sqlite3.connect(state) as db:
            current_row = db.execute("SELECT * FROM threads WHERE id='t1'").fetchone()
            db.execute("ALTER TABLE threads RENAME TO threads_old")
            db.executescript("""
                CREATE TABLE threads (
                  id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL,
                  cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
                  approval_mode TEXT NOT NULL, has_user_event INTEGER NOT NULL DEFAULT 0,
                  archived INTEGER NOT NULL DEFAULT 0, first_user_message TEXT NOT NULL DEFAULT '',
                  preview TEXT NOT NULL DEFAULT '', created_at_ms INTEGER, updated_at_ms INTEGER,
                  git_branch TEXT, thread_source TEXT, required_new TEXT NOT NULL
                );
            """)
            db.execute(
                "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*current_row, "current required value"),
            )
            db.execute("DROP TABLE threads_old")
        # Restore mutates this legacy-relabelled rollout before reaching the incompatible DB row.
        rollout.write_bytes(rollout.read_bytes().replace(b'"model_provider": "custom"', b'"model_provider": "openai"'))
        missing_rollout.unlink()
        before_files = {path: path.read_bytes() for path in (self.home / "config.toml", rollout)}
        db_before = self.sqlite_snapshot(state)
        catalog_before = self.sqlite_snapshot(self.home / "sqlite/codex-dev.db")

        restored = self.run_cli("restore", str(backup), "--no-quit", "--no-open")

        self.assertNotEqual(restored.returncode, 0)
        self.assertIn("NOT NULL", restored.stderr)
        self.assertIn("rolled back", restored.stderr)
        for path, content in before_files.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.sqlite_snapshot(state), db_before)
        self.assertEqual(self.sqlite_snapshot(self.home / "sqlite/codex-dev.db"), catalog_before)
        self.assertFalse(missing_rollout.exists())

    def test_restore_rejects_provider_removed_from_current_config_without_data_changes(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom", encrypted=True)
        state = self.home / "state_5.sqlite"
        self.create_state(state, "t1", "custom")
        self.create_catalog(self.home / "sqlite/codex-dev.db")
        self.insert_catalog("t1", "custom")
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        self.write_config("openai", include_custom=False)
        before_files = {path: path.read_bytes() for path in (self.home / "config.toml", rollout)}
        db_before = self.sqlite_snapshot(state)
        catalog_before = self.sqlite_snapshot(self.home / "sqlite/codex-dev.db")

        restored = self.run_cli("restore", str(backup), "--no-quit", "--no-open")

        self.assertNotEqual(restored.returncode, 0)
        self.assertIn("backup provider 'custom' is not configured", restored.stderr)
        for path, content in before_files.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.sqlite_snapshot(state), db_before)
        self.assertEqual(self.sqlite_snapshot(self.home / "sqlite/codex-dev.db"), catalog_before)

    def test_restore_appends_normalized_backup_suffix_to_complete_native_current(self):
        self.write_config("custom")
        message = {
            "type": "message", "role": "assistant", "id": "msg_native",
            "content": [{"type": "output_text", "text": "original answer"}],
        }
        rollout = self.write_rollout("openai", encrypted=True, response_items=(message,))
        complete = rollout.read_bytes()
        records = list(map(json.loads, complete.splitlines()))
        records[0]["payload"]["model_provider"] = "custom"
        portable_message = {key: value for key, value in message.items() if key != "id"}
        normalized = [records[0], records[1], {"type": "response_item", "payload": portable_message}]
        suffix = json.dumps({
            "type": "response_item", "payload": {
                "type": "reasoning", "id": "rs_custom_new", "encrypted_content": "new custom native state",
            },
        }).encode() + b"\n"
        rollout.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in normalized) + suffix)
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(version=3, operation="pre-restore")
        manifest_path.write_text(json.dumps(manifest) + "\n")
        rollout.write_bytes(complete)

        restored = self.run_cli("restore", str(backup), "--no-quit", "--no-open")

        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(rollout.read_bytes(), complete + suffix)

    def test_restore_completes_shorter_exact_native_prefix(self):
        self.write_config("custom")
        rollout = self.write_rollout("custom", encrypted=True, response_items=({
            "type": "message", "role": "assistant", "id": "msg_native",
            "content": [{"type": "output_text", "text": "later original answer"}],
        },))
        complete = rollout.read_bytes()
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        rollout.write_bytes(b"".join(complete.splitlines(keepends=True)[:3]))

        restored = self.run_cli("restore", str(backup), "--no-quit", "--no-open")

        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(rollout.read_bytes(), complete)

    def test_default_switch_does_not_prune_backups(self):
        self.write_config("custom")
        self.write_rollout("custom", encrypted=True)
        for _ in range(6):
            switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
            self.assertEqual(switched.returncode, 0, switched.stderr)
        self.assertEqual(len(MODULE.backup_paths(self.home)), 6)

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

    def insert_catalog(self, thread_id, provider, title="Title", host_id="local"):
        with sqlite3.connect(self.home / "sqlite/codex-dev.db") as db:
            db.execute(
                """INSERT INTO local_thread_catalog
                   (host_id,thread_id,display_title,source_created_at,source_updated_at,cwd,
                    source_kind,source_detail,model_provider,git_branch,observation_sequence,missing_candidate)
                   VALUES (?,?,?,1,2,'/tmp/project','app',NULL,?,NULL,1,0)""",
                (host_id, thread_id, title, provider),
            )

    def assert_restore_skips_unmatched_backup_rows(self, *, missing_destination):
        self.write_config("custom")
        rollout = self.write_rollout("custom", encrypted=True)
        state = self.home / "state_5.sqlite"
        catalog = self.home / "sqlite/codex-dev.db"
        self.create_state(state, "t1", "custom")
        self.set_thread_rollout(state, "t1", rollout)
        self.insert_thread(state, "backup-only-unmatched", "custom")
        self.create_catalog(catalog)
        self.insert_catalog("t1", "custom")
        self.insert_catalog("backup-only-unmatched", "custom")
        self.insert_catalog("remote-only", "custom", host_id="remote")
        switched = self.run_cli("switch", "openai", "--no-quit", "--no-open")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        backup = self.result_backup(switched)
        if missing_destination:
            state.unlink()
            catalog.unlink()
        else:
            with sqlite3.connect(state) as db:
                db.execute("DELETE FROM threads WHERE id='backup-only-unmatched'")
            with sqlite3.connect(catalog) as db:
                db.execute("DELETE FROM local_thread_catalog WHERE thread_id='backup-only-unmatched'")
            self.insert_thread(state, "current-only-unmatched", "openai")
            self.insert_catalog("current-only-unmatched", "openai")

        restored = self.run_cli("restore", str(backup), "--no-quit", "--no-open")

        self.assertEqual(restored.returncode, 0, restored.stderr)
        expected = {"t1"} if missing_destination else {"t1", "current-only-unmatched"}
        with sqlite3.connect(state) as db:
            self.assertEqual({row[0] for row in db.execute("SELECT id FROM threads")}, expected)
        with sqlite3.connect(catalog) as db:
            rows = db.execute("SELECT host_id,thread_id FROM local_thread_catalog").fetchall()
        if missing_destination:
            self.assertEqual(set(rows), {("local", "t1"), ("remote", "remote-only")})
        else:
            self.assertEqual(
                set(rows),
                {("local", "t1"), ("local", "current-only-unmatched"), ("remote", "remote-only")},
            )

    @staticmethod
    def set_thread_rollout(path, thread_id, rollout):
        with sqlite3.connect(path) as db:
            db.execute(
                "UPDATE threads SET rollout_path=? WHERE id=?",
                (str(rollout), thread_id),
            )

    @staticmethod
    def result_backup(result):
        backup_line = next(line for line in result.stdout.splitlines() if line.startswith("Backup: "))
        return Path(backup_line.removeprefix("Backup: "))

    @staticmethod
    def sqlite_snapshot(path):
        with sqlite3.connect(path) as db:
            schema = tuple(db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall())
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )]
            rows = tuple(
                (table, tuple(db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()))
                for table in tables
            )
            return schema, rows


if __name__ == "__main__":
    unittest.main()

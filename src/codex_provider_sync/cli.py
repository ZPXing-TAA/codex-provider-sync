"""Safely synchronize Codex provider metadata and desktop history indexes."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Iterable, Mapping

from . import __version__
from .history import normalize_response_history

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


VERSION = __version__
DEFAULT_PROVIDER = "openai"
SESSION_DIRS = ("sessions", "archived_sessions")
DB_RELATIVE_PATHS = (Path("state_5.sqlite"), Path("sqlite/state_5.sqlite"))
CATALOG_RELATIVE_PATH = Path("sqlite/codex-dev.db")
BACKUP_SUFFIX = "-codex-switch"
ENCRYPTED_TOKEN = b'"encrypted_content"'
PROVIDER_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
ROOT_PROVIDER_LINE = re.compile(r"^(?P<indent>\s*)model_provider\s*=.*$")
TABLE_HEADER_LINE = re.compile(r"^\s*\[\[?.+?\]\]?\s*(?:#.*)?$")


@dataclass(frozen=True)
class ConfigState:
    provider: str
    configured_providers: frozenset[str]
    text: str


@dataclass(frozen=True)
class RolloutInfo:
    path: Path
    provider: str
    encrypted: bool
    response_items: int


@dataclass(frozen=True)
class RolloutScan:
    items: tuple[RolloutInfo, ...]
    unreadable: tuple[tuple[Path, str], ...]


@dataclass(frozen=True)
class RolloutUpdate:
    changed: bool
    removed_items: int = 0
    converted_items: int = 0
    stripped_ids: int = 0


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def atomic_write(path: Path, content: bytes, mode: int | None = None) -> None:
    temp = path.with_name(f".{path.name}.codex-switch-{os.getpid()}")
    try:
        with temp.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not permit directory fsync.
            pass
    finally:
        temp.unlink(missing_ok=True)


def read_config(config_path: Path) -> ConfigState:
    text = config_path.read_text(encoding="utf-8")
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise RuntimeError(f"invalid TOML in {config_path}: {error}") from error
    provider = parsed.get("model_provider", DEFAULT_PROVIDER)
    if not isinstance(provider, str) or not PROVIDER_PATTERN.fullmatch(provider):
        raise RuntimeError(f"invalid root model_provider in {config_path}: {provider!r}")
    configured = {DEFAULT_PROVIDER}
    model_providers = parsed.get("model_providers", {})
    if isinstance(model_providers, dict):
        configured.update(key for key in model_providers if isinstance(key, str))
    return ConfigState(provider, frozenset(configured), text)


def render_config_with_provider(config_path: Path, state: ConfigState, provider: str) -> bytes:
    if not PROVIDER_PATTERN.fullmatch(provider):
        raise ValueError(f"invalid provider id: {provider!r}")
    lines = state.text.splitlines(keepends=True)
    root_end = len(lines)
    for index, line in enumerate(lines):
        if TABLE_HEADER_LINE.match(line):
            root_end = index
            break
    replaced = False
    for index in range(root_end):
        match = ROOT_PROVIDER_LINE.match(lines[index].rstrip("\r\n"))
        if not match:
            continue
        ending = "\r\n" if lines[index].endswith("\r\n") else "\n"
        lines[index] = f'{match.group("indent")}model_provider = "{provider}"{ending}'
        replaced = True
        break
    if not replaced:
        insertion = f'model_provider = "{provider}"\n'
        if root_end > 0 and not lines[root_end - 1].endswith(("\n", "\r")):
            lines[root_end - 1] += "\n"
        if root_end > 0 and lines[root_end - 1].strip():
            insertion += "\n"
        lines.insert(root_end, insertion)
    rendered = "".join(lines)
    try:
        parsed = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as error:
        raise RuntimeError(f"refusing to write invalid TOML to {config_path}: {error}") from error
    if parsed.get("model_provider") != provider:
        raise RuntimeError("failed to set the root model_provider without changing nested tables")
    return rendered.encode("utf-8")


def set_config_provider(config_path: Path, state: ConfigState, provider: str) -> None:
    content = render_config_with_provider(config_path, state, provider)
    atomic_write(config_path, content, config_path.stat().st_mode & 0o7777)


def sqlite_tables(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as error:
        raise RuntimeError(f"cannot read SQLite database {path}: {error}") from error


def connection_tables(db: sqlite3.Connection, schema: str = "main") -> set[str]:
    return {
        row[0]
        for row in db.execute(
            f"SELECT name FROM {quote_identifier(schema)}.sqlite_master WHERE type='table'"
        )
    }


def table_columns(db: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [
        row[1]
        for row in db.execute(
            f"PRAGMA {quote_identifier(schema)}.table_info({quote_identifier(table)})"
        )
    ]


def validate_database(path: Path, required_table: str) -> None:
    if not path.exists():
        return
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15) as db:
            result = db.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"SQLite quick_check failed for {path}: {result}")
            if required_table not in connection_tables(db):
                raise RuntimeError(f"required table {required_table!r} is missing from {path}")
    except sqlite3.Error as error:
        raise RuntimeError(f"cannot validate SQLite database {path}: {error}") from error


def thread_db_rank(path: Path) -> tuple[int, int]:
    if "threads" not in sqlite_tables(path):
        return (0, 0)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        columns = set(table_columns(db, "main", "threads"))
        recency = "COALESCE(NULLIF(updated_at_ms,0),updated_at*1000)" if "updated_at_ms" in columns else "updated_at*1000"
        count, latest = db.execute(f"SELECT COUNT(*),COALESCE(MAX({recency}),0) FROM threads").fetchone()
        return int(count), int(latest)


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source, timeout=15) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.codex-switch-{os.getpid()}")
    try:
        shutil.copy2(source, temp)
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def safe_relative_path(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"unsafe path in backup manifest: {value!r}")
    return relative


def backup_root(codex_home: Path) -> Path:
    return codex_home / "recovery_backups"


def backup_paths(codex_home: Path) -> list[Path]:
    root = backup_root(codex_home)
    if not root.exists():
        return []
    return sorted(
        (path for path in root.glob(f"*{BACKUP_SUFFIX}") if (path / "manifest.json").is_file()),
        reverse=True,
    )


def create_backup(
    codex_home: Path,
    changed_rollouts: Iterable[Path],
    *,
    operation: str,
    source_provider: str,
    target_provider: str,
    source_provider_counts: Mapping[str, int] | None = None,
) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_root(codex_home) / f"{stamp}{BACKUP_SUFFIX}"
    partial = backup.with_name(f".{backup.name}.partial")
    partial.mkdir(parents=True)
    try:
        config = codex_home / "config.toml"
        if config.exists():
            shutil.copy2(config, partial / "config.toml")
        backed_up_databases: list[str] = []
        absent_databases: list[str] = []
        for relative in (*DB_RELATIVE_PATHS, CATALOG_RELATIVE_PATH):
            source = codex_home / relative
            if source.exists():
                sqlite_backup(source, partial / relative)
                backed_up_databases.append(str(relative))
            else:
                absent_databases.append(str(relative))
        manifest_rollouts: list[str] = []
        absent_rollouts: list[str] = []
        for source in sorted(set(changed_rollouts)):
            relative = source.relative_to(codex_home)
            if not source.exists():
                absent_rollouts.append(str(relative))
                continue
            destination = partial / "rollouts" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            manifest_rollouts.append(str(relative))
        counts = dict(source_provider_counts or {})
        if counts:
            source_provider = next(iter(counts)) if len(counts) == 1 else "(mixed)"
        manifest = {
            "version": 3,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "operation": operation,
            "source_provider": source_provider,
            "source_provider_counts": counts,
            "target_provider": target_provider,
            "databases": backed_up_databases,
            "absent_databases": absent_databases,
            "rollouts": manifest_rollouts,
            "absent_rollouts": absent_rollouts,
        }
        (partial / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(partial, backup)
        return backup
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def read_backup_manifest(backup: Path) -> dict:
    manifest_path = backup / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"backup manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read backup manifest {manifest_path}: {error}") from error
    if not isinstance(manifest.get("rollouts", []), list):
        raise RuntimeError(f"invalid rollout list in {manifest_path}")
    return manifest


def restore_backup(codex_home: Path, backup: Path) -> None:
    manifest = read_backup_manifest(backup)
    config = backup / "config.toml"
    if config.exists():
        atomic_copy(config, codex_home / "config.toml")
    database_values = manifest.get("databases")
    if not isinstance(database_values, list):
        # Version 1 backups predate the explicit database list.
        database_values = [str(relative) for relative in (*DB_RELATIVE_PATHS, CATALOG_RELATIVE_PATH)]
    for value in database_values:
        relative = safe_relative_path(str(value))
        source = backup / relative
        if not source.exists():
            continue
        destination = codex_home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(destination) + suffix).unlink(missing_ok=True)
        atomic_copy(source, destination)
    for value in manifest.get("absent_databases", []):
        relative = safe_relative_path(str(value))
        destination = codex_home / relative
        for suffix in ("", "-wal", "-shm"):
            Path(str(destination) + suffix).unlink(missing_ok=True)
    for value in manifest.get("rollouts", []):
        relative = safe_relative_path(str(value))
        source = backup / "rollouts" / relative
        if not source.is_file():
            raise RuntimeError(f"rollout is missing from backup: {source}")
        destination = codex_home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy(source, destination)
    for value in manifest.get("absent_rollouts", []):
        relative = safe_relative_path(str(value))
        (codex_home / relative).unlink(missing_ok=True)


def prune_backups(codex_home: Path, keep: int) -> int:
    removed = 0
    for old in backup_paths(codex_home)[keep:]:
        shutil.rmtree(old)
        removed += 1
    return removed


def rollout_paths(codex_home: Path) -> list[Path]:
    paths: list[Path] = []
    for dirname in SESSION_DIRS:
        root = codex_home / dirname
        if root.exists():
            paths.extend(root.rglob("*.jsonl"))
    return sorted(paths)


def scan_rollouts(codex_home: Path) -> RolloutScan:
    items: list[RolloutInfo] = []
    unreadable: list[tuple[Path, str]] = []
    for path in rollout_paths(codex_home):
        try:
            with path.open("rb") as handle:
                first = handle.readline()
                encrypted = ENCRYPTED_TOKEN in first
                response_items = 0
                for line_number, line in enumerate(handle, 2):
                    encrypted = encrypted or ENCRYPTED_TOKEN in line
                    if b"response_item" not in line:
                        continue
                    record = json.loads(line)
                    if record.get("type") == "response_item":
                        if not isinstance(record.get("payload"), dict):
                            raise ValueError(
                                f"response_item payload on line {line_number} is not an object"
                            )
                        response_items += 1
            item = json.loads(first)
            if item.get("type") != "session_meta" or not isinstance(item.get("payload"), dict):
                raise ValueError("first JSONL item is not session_meta")
            provider = item["payload"].get("model_provider", "(missing)")
            if not isinstance(provider, str):
                provider = "(invalid)"
            items.append(RolloutInfo(path, provider, encrypted, response_items))
        except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            unreadable.append((path, str(error)))
    return RolloutScan(tuple(items), tuple(unreadable))


def changed_rollouts(scan: RolloutScan, provider: str) -> list[RolloutInfo]:
    return [item for item in scan.items if item.provider != provider]


def update_rollout(path: Path, provider: str) -> RolloutUpdate:
    lines = path.read_bytes().splitlines(keepends=True)
    if not lines:
        raise RuntimeError(f"rollout changed during synchronization: {path}")
    item = json.loads(lines[0])
    if item.get("type") != "session_meta" or not isinstance(item.get("payload"), dict):
        raise RuntimeError(f"rollout changed during synchronization: {path}")
    payload = item["payload"]
    if payload.get("model_provider") == provider:
        return RolloutUpdate(False)
    source_provider = payload.get("model_provider")
    if not isinstance(source_provider, str):
        source_provider = None
    payload["model_provider"] = provider
    output = [json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"]
    removed_items = 0
    converted_items = 0
    stripped_ids = 0
    for line_number, line in enumerate(lines[1:], 2):
        if b"response_item" not in line:
            output.append(line)
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"invalid JSON on line {line_number} of {path}: {error}") from error
        if record.get("type") != "response_item":
            output.append(line)
            continue
        response_payload = record.get("payload")
        if not isinstance(response_payload, dict):
            raise RuntimeError(
                f"response_item payload on line {line_number} of {path} is not an object"
            )
        normalized = normalize_response_history(
            [response_payload],
            source_provider=source_provider,
            target_provider=provider,
        )
        if not normalized.items:
            removed_items += 1
            continue
        stripped_ids += sum(
            diagnostic.action == "stripped-id" for diagnostic in normalized.diagnostics
        )
        for normalized_item in normalized.items:
            if normalized_item.get("type") != response_payload.get("type"):
                converted_items += 1
            normalized_record = dict(record)
            normalized_record["payload"] = normalized_item
            output.append(
                json.dumps(normalized_record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
    atomic_write(path, b"".join(output), path.stat().st_mode & 0o7777)
    return RolloutUpdate(True, removed_items, converted_items, stripped_ids)


def confirm_history_normalization(changes: list[RolloutInfo], assume_yes: bool) -> bool:
    risky = [item for item in changes if item.response_items]
    if not risky:
        return assume_yes
    if assume_yes:
        return True
    message = (
        f"{len(risky)} rollout file(s) contain provider-native response items from a different "
        "provider. The switch will keep portable messages, strip provider-generated IDs, and "
        "remove opaque reasoning and tool continuation state after creating a rollback backup."
    )
    print(f"WARNING: {message}", file=sys.stderr)
    if not sys.stdin.isatty():
        raise RuntimeError("confirmation required; review the normalization warning and rerun with --yes")
    answer = input("Continue after creating a rollback backup? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        raise RuntimeError("cancelled by user")
    return True


def source_recency_expression(alias: str, columns: set[str]) -> str:
    if "updated_at_ms" in columns:
        return f"COALESCE(NULLIF({alias}.updated_at_ms,0),{alias}.updated_at*1000)"
    return f"{alias}.updated_at*1000"


def copy_thread_rows(destination: Path, source: Path, *, overwrite_existing: bool) -> tuple[int, int]:
    with sqlite3.connect(destination, timeout=15) as db:
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("ATTACH DATABASE ? AS source_db", (str(source),))
        try:
            destination_columns = set(table_columns(db, "main", "threads"))
            source_columns = set(table_columns(db, "source_db", "threads"))
            common = [
                column
                for column in table_columns(db, "main", "threads")
                if column in source_columns
            ]
            if "id" not in destination_columns or "id" not in source_columns:
                raise RuntimeError("threads table does not contain an id column")
            quoted = ",".join(quote_identifier(column) for column in common)
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                f"INSERT OR IGNORE INTO main.threads ({quoted}) SELECT {quoted} FROM source_db.threads"
            ).rowcount
            mutable = [column for column in common if column != "id"]
            assignments = ",".join(
                f"{quote_identifier(column)}=(SELECT s.{quote_identifier(column)} "
                f"FROM source_db.threads s WHERE s.id=threads.id)"
                for column in mutable
            )
            condition = ""
            if overwrite_existing:
                differences = " OR ".join(
                    f"s.{quote_identifier(column)} IS NOT threads.{quote_identifier(column)}"
                    for column in mutable
                )
                condition = f" AND ({differences})"
            else:
                source_recency = source_recency_expression("s", source_columns)
                destination_recency = source_recency_expression("threads", destination_columns)
                condition = f" AND {source_recency}>{destination_recency}"
            updated = db.execute(
                f"UPDATE main.threads AS threads SET {assignments} "
                "WHERE EXISTS (SELECT 1 FROM source_db.threads s "
                f"WHERE s.id=threads.id{condition})"
            ).rowcount
            db.commit()
            return inserted, updated
        finally:
            db.execute("DETACH DATABASE source_db")


def merge_thread_dbs(first: Path, second: Path) -> tuple[Path | None, int, int]:
    existing = [path for path in (first, second) if path.exists()]
    if not existing:
        return None, 0, 0
    if len(existing) == 1:
        return existing[0], 0, 0
    primary = max(existing, key=thread_db_rank)
    secondary = second if primary == first else first
    inserted_primary, updated_primary = copy_thread_rows(
        primary, secondary, overwrite_existing=False
    )
    inserted_secondary, updated_secondary = copy_thread_rows(
        secondary, primary, overwrite_existing=True
    )
    return (
        primary,
        inserted_primary + inserted_secondary,
        updated_primary + updated_secondary,
    )


def update_thread_db(path: Path, provider: str) -> tuple[int, int]:
    if not path.exists():
        return (0, 0)
    with sqlite3.connect(path, timeout=15) as db:
        db.execute("PRAGMA busy_timeout=15000")
        columns = set(table_columns(db, "main", "threads"))
        if "model_provider" not in columns:
            raise RuntimeError(f"threads.model_provider is missing from {path}")
        db.execute("BEGIN IMMEDIATE")
        provider_changes = db.execute(
            "UPDATE threads SET model_provider=? WHERE COALESCE(model_provider,'')<>?",
            (provider, provider),
        ).rowcount
        visible_changes = 0
        if {"has_user_event", "preview", "first_user_message"}.issubset(columns):
            visible_changes = db.execute(
                """UPDATE threads SET has_user_event=1
                   WHERE COALESCE(has_user_event,0)<>1
                     AND (COALESCE(preview,'')<>'' OR COALESCE(first_user_message,'')<>'')"""
            ).rowcount
        db.commit()
    return provider_changes, visible_changes


def rebuild_catalog(codex_home: Path, provider: str, state_path: Path | None) -> int:
    catalog_path = codex_home / CATALOG_RELATIVE_PATH
    if not catalog_path.exists() or state_path is None:
        return 0
    with sqlite3.connect(state_path) as state:
        columns = set(table_columns(state, "main", "threads"))
        required = {"id", "title", "created_at", "updated_at", "cwd", "source", "archived"}
        missing = required - columns
        if missing:
            raise RuntimeError(f"unsupported threads schema in {state_path}; missing {sorted(missing)}")
        created = "COALESCE(NULLIF(created_at_ms,0),created_at*1000)" if "created_at_ms" in columns else "created_at*1000"
        recency = "COALESCE(NULLIF(recency_at_ms,0),NULLIF(updated_at_ms,0),updated_at*1000)" if "recency_at_ms" in columns else ("COALESCE(NULLIF(updated_at_ms,0),updated_at*1000)" if "updated_at_ms" in columns else "updated_at*1000")
        preview = "COALESCE(preview,'')" if "preview" in columns else "''"
        first_message = "COALESCE(first_user_message,'')" if "first_user_message" in columns else "''"
        user_event = "COALESCE(has_user_event,0)=1" if "has_user_event" in columns else "0"
        thread_source = "NULLIF(thread_source,'')" if "thread_source" in columns else "NULL"
        git_branch = "git_branch" if "git_branch" in columns else "NULL"
        rows = state.execute(
            f"""SELECT id,COALESCE(NULLIF(title,''),NULLIF({preview},''),id),
                       {created}/1000.0,{recency}/1000.0,cwd,source,
                       {thread_source},{git_branch}
                FROM threads
                WHERE archived=0 AND ({preview}<>'' OR {first_message}<>'' OR {user_event})
                ORDER BY {recency},id"""
        ).fetchall()
    with sqlite3.connect(catalog_path, timeout=15) as catalog:
        catalog.execute("PRAGMA busy_timeout=15000")
        tables = connection_tables(catalog)
        if "local_thread_catalog" not in tables:
            raise RuntimeError(f"local_thread_catalog is missing from {catalog_path}")
        required_catalog = {
            "host_id", "thread_id", "display_title", "source_created_at",
            "source_updated_at", "cwd", "source_kind", "source_detail",
            "model_provider", "git_branch", "observation_sequence", "missing_candidate",
        }
        catalog_columns = set(table_columns(catalog, "main", "local_thread_catalog"))
        missing_catalog = required_catalog - catalog_columns
        if missing_catalog:
            raise RuntimeError(
                f"unsupported local_thread_catalog schema in {catalog_path}; missing {sorted(missing_catalog)}"
            )
        catalog.execute("BEGIN IMMEDIATE")
        catalog.execute("DELETE FROM local_thread_catalog WHERE host_id='local'")
        catalog.executemany(
            """INSERT INTO local_thread_catalog
               (host_id,thread_id,display_title,source_created_at,source_updated_at,cwd,
                source_kind,source_detail,model_provider,git_branch,observation_sequence,missing_candidate)
               VALUES ('local',?,?,?,?,?,?,?,?,?,?,0)""",
            [(*row[:7], provider, row[7], index) for index, row in enumerate(rows, 1)],
        )
        if "local_thread_catalog_sync_state" in tables:
            watermark = max((row[3] for row in rows), default=0)
            catalog.execute(
                """INSERT INTO local_thread_catalog_sync_state
                   (host_id,watermark_updated_at,initial_build_complete,observation_sequence)
                   VALUES ('local',?,1,?)
                   ON CONFLICT(host_id) DO UPDATE SET
                     watermark_updated_at=excluded.watermark_updated_at,
                     initial_build_complete=1,
                     observation_sequence=excluded.observation_sequence""",
                (watermark, len(rows)),
            )
        catalog.commit()
    return len(rows)


def provider_counts(codex_home: Path) -> dict:
    scan = scan_rollouts(codex_home)
    rollout_counts = Counter(item.provider for item in scan.items)
    encrypted_counts = Counter(item.provider for item in scan.items if item.encrypted)
    response_item_rollouts = Counter(item.provider for item in scan.items if item.response_items)
    response_items = Counter()
    for item in scan.items:
        if item.response_items:
            response_items[item.provider] += item.response_items
    result: dict = {
        "rollouts": dict(rollout_counts),
        "encrypted_rollouts": dict(encrypted_counts),
        "response_item_rollouts": dict(response_item_rollouts),
        "response_items": dict(response_items),
        "unreadable_rollouts": len(scan.unreadable),
        "databases": {},
        "catalog": {},
    }
    for relative in DB_RELATIVE_PATHS:
        path = codex_home / relative
        counts: dict[str, int] = {}
        if path.exists() and "threads" in sqlite_tables(path):
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                for provider, count in db.execute("SELECT model_provider,COUNT(*) FROM threads GROUP BY 1"):
                    counts[provider or "(missing)"] = count
        result["databases"][str(relative)] = counts
    catalog_path = codex_home / CATALOG_RELATIVE_PATH
    if catalog_path.exists() and "local_thread_catalog" in sqlite_tables(catalog_path):
        with sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True) as db:
            for provider, count in db.execute(
                "SELECT model_provider,COUNT(*) FROM local_thread_catalog GROUP BY 1"
            ):
                result["catalog"][provider or "(missing)"] = count
    return result


def is_codex_running() -> bool:
    return subprocess.run(
        ["pgrep", "-x", "Codex"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def quit_codex() -> None:
    subprocess.run(
        ["osascript", "-e", 'quit app "Codex"'],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if not is_codex_running():
            return
        time.sleep(0.5)
    raise RuntimeError("Codex did not exit within 20 seconds; close it manually and retry")


def open_codex() -> None:
    subprocess.run(["open", "-a", "Codex"], check=True)


def validate_state_files(codex_home: Path) -> None:
    for relative in DB_RELATIVE_PATHS:
        validate_database(codex_home / relative, "threads")
    catalog = codex_home / CATALOG_RELATIVE_PATH
    if catalog.exists():
        validate_database(catalog, "local_thread_catalog")


def rollback_or_raise(codex_home: Path, backup: Path, original: Exception) -> None:
    try:
        restore_backup(codex_home, backup)
    except Exception as rollback_error:
        raise RuntimeError(
            f"operation failed: {original}; rollback also failed: {rollback_error}; "
            f"manual backup: {backup}"
        ) from original
    raise RuntimeError(f"operation failed and was rolled back from {backup}: {original}") from original


def run_sync(args: argparse.Namespace) -> None:
    codex_home = Path(args.codex_home).expanduser().resolve()
    config_path = codex_home / "config.toml"
    config = read_config(config_path)
    if config.provider not in config.configured_providers:
        available = ", ".join(sorted(config.configured_providers))
        raise RuntimeError(
            f"config.toml selects provider {config.provider!r}, but that provider is not configured; "
            f"available: {available}"
        )
    target_provider = config.provider
    update_config = args.command == "switch"
    if args.command == "sync" and args.provider:
        if args.provider != config.provider:
            raise RuntimeError(
                f"config.toml currently uses {config.provider!r}; sync cannot target {args.provider!r}. "
                f"Configure that provider first or use `codex-switch switch {args.provider}`."
            )
        target_provider = args.provider
    elif update_config:
        target_provider = args.provider
        if target_provider not in config.configured_providers:
            available = ", ".join(sorted(config.configured_providers))
            raise RuntimeError(
                f"provider {target_provider!r} is not configured in config.toml; available: {available}"
            )
    initial_scan = scan_rollouts(codex_home)
    if initial_scan.unreadable:
        preview = "; ".join(f"{path}: {error}" for path, error in initial_scan.unreadable[:3])
        raise RuntimeError(
            f"refusing to continue because {len(initial_scan.unreadable)} rollout file(s) are unreadable: {preview}"
        )
    risk_accepted = confirm_history_normalization(
        changed_rollouts(initial_scan, target_provider), args.yes
    )
    if args.no_quit and codex_home == Path("~/.codex").expanduser().resolve() and is_codex_running():
        raise RuntimeError("refusing to modify the live default CODEX_HOME with --no-quit")

    lifecycle_started = False
    backup: Path | None = None
    result: dict | None = None
    operation_error: Exception | None = None
    try:
        if not args.no_quit:
            lifecycle_started = True
            quit_codex()
        validate_state_files(codex_home)
        scan = scan_rollouts(codex_home)
        if scan.unreadable:
            raise RuntimeError(f"{len(scan.unreadable)} rollout file(s) became unreadable")
        changes = changed_rollouts(scan, target_provider)
        confirm_history_normalization(changes, risk_accepted)
        source_provider_counts = Counter(item.provider for item in changes)
        backup = create_backup(
            codex_home,
            (item.path for item in changes),
            operation=args.command,
            source_provider=config.provider,
            target_provider=target_provider,
            source_provider_counts=source_provider_counts,
        )
        try:
            if update_config:
                set_config_provider(config_path, config, target_provider)
            rollout_updates = [update_rollout(item.path, target_provider) for item in changes]
            changed_count = sum(update.changed for update in rollout_updates)
            first, second = (codex_home / relative for relative in DB_RELATIVE_PATHS)
            state_path, inserted_rows, reconciled_rows = merge_thread_dbs(first, second)
            db_updates = [
                update_thread_db(path, target_provider)
                for path in (first, second)
                if path.exists()
            ]
            catalog_rows = rebuild_catalog(codex_home, target_provider, state_path)
            validate_state_files(codex_home)
            result = {
                "changed_count": changed_count,
                "removed_items": sum(update.removed_items for update in rollout_updates),
                "converted_items": sum(update.converted_items for update in rollout_updates),
                "stripped_ids": sum(update.stripped_ids for update in rollout_updates),
                "inserted_rows": inserted_rows,
                "reconciled_rows": reconciled_rows,
                "provider_rows": sum(item[0] for item in db_updates),
                "visibility_rows": sum(item[1] for item in db_updates),
                "catalog_rows": catalog_rows,
            }
        except Exception as error:
            rollback_or_raise(codex_home, backup, error)
    except Exception as error:
        operation_error = error
    finally:
        if lifecycle_started and not args.no_open:
            try:
                open_codex()
            except Exception as reopen_error:
                if operation_error is None:
                    operation_error = RuntimeError(
                        f"synchronization succeeded, but Codex could not be reopened: {reopen_error}"
                    )
                else:
                    print(f"Warning: Codex could not be reopened: {reopen_error}", file=sys.stderr)
    if operation_error is not None:
        raise operation_error
    assert backup is not None and result is not None
    try:
        removed_backups = prune_backups(codex_home, args.keep)
    except Exception as prune_error:
        removed_backups = 0
        print(f"Warning: old backups could not be pruned: {prune_error}", file=sys.stderr)
    print(f"Provider synchronized: {target_provider}")
    print(f"Backup: {backup}")
    print(f"Rollout files updated: {result['changed_count']}")
    print(f"Provider-native response items removed: {result['removed_items']}")
    print(f"Reasoning summaries converted to messages: {result['converted_items']}")
    print(f"Provider-generated IDs stripped: {result['stripped_ids']}")
    print(f"SQLite rows inserted: {result['inserted_rows']}")
    print(f"SQLite rows reconciled: {result['reconciled_rows']}")
    print(f"SQLite provider rows updated: {result['provider_rows']}")
    print(f"SQLite visibility flags repaired: {result['visibility_rows']}")
    print(f"Sidebar catalog rows rebuilt: {result['catalog_rows']}")
    print(f"Old backups removed: {removed_backups}")


def resolve_backup(codex_home: Path, value: str | None) -> Path:
    if value:
        backup = Path(value).expanduser().resolve()
    else:
        backups = backup_paths(codex_home)
        if not backups:
            raise RuntimeError("no codex-switch backups were found")
        backup = backups[0]
    read_backup_manifest(backup)
    return backup


def run_restore(args: argparse.Namespace) -> None:
    codex_home = Path(args.codex_home).expanduser().resolve()
    selected = resolve_backup(codex_home, args.backup)
    manifest = read_backup_manifest(selected)
    selected_rollouts = [
        codex_home / safe_relative_path(str(value))
        for value in [
            *manifest.get("rollouts", []),
            *manifest.get("absent_rollouts", []),
        ]
    ]
    lifecycle_started = False
    safety_backup: Path | None = None
    operation_error: Exception | None = None
    try:
        if not args.no_quit:
            lifecycle_started = True
            quit_codex()
        validate_state_files(codex_home)
        current = read_config(codex_home / "config.toml")
        safety_backup = create_backup(
            codex_home,
            selected_rollouts,
            operation="pre-restore",
            source_provider=current.provider,
            target_provider=str(manifest.get("source_provider", "unknown")),
        )
        try:
            restore_backup(codex_home, selected)
            validate_state_files(codex_home)
        except Exception as error:
            rollback_or_raise(codex_home, safety_backup, error)
    except Exception as error:
        operation_error = error
    finally:
        if lifecycle_started and not args.no_open:
            try:
                open_codex()
            except Exception as reopen_error:
                if operation_error is None:
                    operation_error = RuntimeError(
                        f"restore succeeded, but Codex could not be reopened: {reopen_error}"
                    )
                else:
                    print(f"Warning: Codex could not be reopened: {reopen_error}", file=sys.stderr)
    if operation_error is not None:
        raise operation_error
    print(f"Restored backup: {selected}")
    print(f"Safety backup of the pre-restore state: {safety_backup}")


def add_lifecycle_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-quit", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-open", action="store_true", help="do not reopen Codex")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-switch", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", "~/.codex"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="show provider metadata across all history layers")
    status.add_argument("--json", action="store_true")
    subparsers.add_parser("backups", help="list rollback backups")
    sync = subparsers.add_parser("sync", help="sync history to the provider already active in config.toml")
    sync.add_argument("provider", nargs="?", help=argparse.SUPPRESS)
    sync.add_argument("--yes", action="store_true", help="accept provider-state normalization")
    sync.add_argument("--keep", type=int, default=5, help="number of rollback backups to retain")
    add_lifecycle_options(sync)
    switch = subparsers.add_parser("switch", help="switch to a provider configured in config.toml and sync history")
    switch.add_argument("provider")
    switch.add_argument("--yes", action="store_true", help="accept provider-state normalization")
    switch.add_argument("--keep", type=int, default=5, help="number of rollback backups to retain")
    add_lifecycle_options(switch)
    restore = subparsers.add_parser("restore", help="restore a rollback backup (latest by default)")
    restore.add_argument("backup", nargs="?")
    add_lifecycle_options(restore)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    codex_home = Path(args.codex_home).expanduser().resolve()
    if args.command == "status":
        config = read_config(codex_home / "config.toml")
        data = {
            "codex_home": str(codex_home),
            "config_provider": config.provider,
            "configured_providers": sorted(config.configured_providers),
            **provider_counts(codex_home),
        }
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"Codex home: {codex_home}")
            print(f"Config provider: {config.provider}")
            print(f"Configured providers: {', '.join(sorted(config.configured_providers))}")
            print(json.dumps({key: value for key, value in data.items() if key not in {"codex_home", "config_provider", "configured_providers"}}, ensure_ascii=False, indent=2))
        return
    if args.command == "backups":
        backups = backup_paths(codex_home)
        if not backups:
            print("No codex-switch backups found.")
            return
        for backup in backups:
            manifest = read_backup_manifest(backup)
            print(
                f"{backup}  {manifest.get('operation', 'sync')}  "
                f"{manifest.get('source_provider', '?')} -> {manifest.get('target_provider', '?')}"
            )
        return
    if hasattr(args, "keep") and args.keep < 1:
        parser.error("--keep must be at least 1")
    lock_path = codex_home / ".codex-switch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another codex-switch process is already running") from error
        if args.command in {"sync", "switch"}:
            run_sync(args)
        elif args.command == "restore":
            run_restore(args)


def main_entry() -> None:
    try:
        main()
    except KeyboardInterrupt:
        print("codex-switch: cancelled", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"codex-switch: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main_entry()

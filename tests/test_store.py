import os
import sqlite3
import stat
from pathlib import Path

from subactor_shell.store import Store


def test_store_persists_sessions_messages_bindings_and_data(tmp_path: Path):
    store = Store(tmp_path / "private" / "state.sqlite3")
    session = store.create_session("test", "mock", "mock")
    store.add_message(session.id, "user", "display", "context", {"x": 1})
    store.bind_secret("TOKEN", "vault://secret/app#token")
    store.set_data("PROJECT", "text", "subactor")

    loaded = Store(store.db_path)
    assert loaded.get_session(session.id).name == "test"  # type: ignore[union-attr]
    assert loaded.list_messages(session.id)[0].context_content == "context"
    assert loaded.get_secret_binding("TOKEN") == "vault://secret/app#token"
    assert loaded.get_data("PROJECT") == ("text", "subactor")

    if os.name == "posix":
        assert stat.S_IMODE(store.db_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.db_path.stat().st_mode) == 0o600


def test_export_contains_each_message_once(tmp_path: Path):
    store = Store(tmp_path / "state.sqlite3")
    session = store.create_session("export", "mock", "mock")
    store.add_message(session.id, "user", "one", "one", {})
    store.add_message(session.id, "assistant", "two", "two", {})

    payload = store.export_session(session.id)

    assert [item["content"] for item in payload["messages"]] == ["one", "two"]


def test_store_migrates_v01_tables_without_losing_existing_rows(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('system', 'user', 'assistant')),
                display_content TEXT NOT NULL,
                context_content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY,
                original_path TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE session_artifacts (
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
                attached_at TEXT NOT NULL,
                PRIMARY KEY(session_id, artifact_id)
            );
            CREATE TABLE secret_bindings (
                alias TEXT PRIMARY KEY,
                secret_ref TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE data_items (
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('text', 'artifact')),
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy", "Legacy", "mock", "mock", "2026-01-01", "2026-01-01"),
        )
        db.execute(
            "INSERT INTO messages(session_id, role, display_content, context_content, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy", "user", "hello", "hello", "{}", "2026-01-01"),
        )
        db.execute(
            "INSERT INTO data_items VALUES (?, ?, ?, ?, ?)",
            ("PROJECT", "text", "subactor", "2026-01-01", "2026-01-01"),
        )
        db.execute(
            "INSERT INTO secret_bindings VALUES (?, ?, ?, ?)",
            (
                "TOKEN",
                "vault://secret/app#token",
                "2026-01-01",
                "2026-01-01",
            ),
        )

    migrated = Store(db_path)

    assert migrated.get_session("legacy") is not None
    assert migrated.list_messages("legacy")[0].display_content == "hello"
    assert migrated.get_data("PROJECT") == ("text", "subactor")
    assert migrated.get_secret_binding("TOKEN") == "vault://secret/app#token"
    assert migrated.usage_summary("legacy")["calls"] == 0
    assert migrated.list_execution_plans("legacy") == []

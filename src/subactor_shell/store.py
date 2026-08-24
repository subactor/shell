from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .config import ensure_private_dir, ensure_private_file
from .models import Artifact, Message, Session, utc_now
from .token_budget import TokenUsage


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path.expanduser()
        ensure_private_dir(self.db_path.parent)
        self._init_schema()
        ensure_private_file(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('system', 'user', 'assistant')),
                    display_content TEXT NOT NULL,
                    context_content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id);

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    original_path TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_artifacts (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
                    attached_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, artifact_id)
                );

                CREATE TABLE IF NOT EXISTS secret_bindings (
                    alias TEXT PRIMARY KEY,
                    secret_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_items (
                    name TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('text', 'artifact')),
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS routing_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    route TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    intent_id TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    candidates_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_routing_session ON routing_decisions(session_id, id);

                CREATE TABLE IF NOT EXISTS provider_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_session ON provider_usage(session_id, id);

                CREATE TABLE IF NOT EXISTS execution_plans (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    intent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_plans_session ON execution_plans(session_id, updated_at);

                CREATE TABLE IF NOT EXISTS execution_receipts (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    ok INTEGER NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_receipts_session ON execution_receipts(session_id, created_at);

                CREATE TABLE IF NOT EXISTS router_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    intent_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_intent ON router_feedback(intent_id, route, id);

                CREATE TABLE IF NOT EXISTS context_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    # Sessions and messages -------------------------------------------------
    def create_session(
        self,
        name: str,
        provider: str,
        model: str,
        session_id: str | None = None,
    ) -> Session:
        session_id = session_id or str(uuid.uuid4())
        now = utc_now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO sessions(id, name, provider, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, name, provider, model, now, now),
            )
        return Session(session_id, name, provider, model, now, now)

    def get_session(self, session_id: str) -> Session | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row_to_session(row) if row else None

    def list_sessions(self, limit: int = 100) -> list[Session]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def update_session(
        self,
        session_id: str,
        *,
        name: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> Session:
        current = self.get_session(session_id)
        if not current:
            raise KeyError(f"Nie ma sesji {session_id}")
        now = utc_now()
        values = (
            name if name is not None else current.name,
            provider if provider is not None else current.provider,
            model if model is not None else current.model,
            now,
            session_id,
        )
        with self._connect() as db:
            db.execute(
                "UPDATE sessions SET name = ?, provider = ?, model = ?, updated_at = ? WHERE id = ?",
                values,
            )
        updated = self.get_session(session_id)
        assert updated is not None
        return updated

    def add_message(
        self,
        session_id: str,
        role: str,
        display_content: str,
        context_content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        if role not in {"system", "user", "assistant"}:
            raise ValueError(f"Nieprawidłowa rola: {role}")
        now = utc_now()
        context_content = display_content if context_content is None else context_content
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO messages(session_id, role, display_content, context_content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, role, display_content, context_content, metadata_json, now),
            )
            db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            message_id = int(cursor.lastrowid)
        return Message(
            message_id,
            session_id,
            role,  # type: ignore[arg-type]
            display_content,
            context_content,
            metadata or {},
            now,
        )

    def list_messages(self, session_id: str) -> list[Message]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def list_messages_recent(self, session_id: str, limit: int = 6) -> list[Message]:
        if limit <= 0:
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row_to_message(row) for row in reversed(rows)]

    # Artifacts, data, secret references -----------------------------------
    def add_artifact(self, artifact: Artifact, session_id: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO artifacts(id, original_path, stored_path, mime_type, size, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    artifact.original_path,
                    str(artifact.stored_path),
                    artifact.mime_type,
                    artifact.size,
                    artifact.created_at,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO session_artifacts(session_id, artifact_id, attached_at) VALUES (?, ?, ?)",
                (session_id, artifact.id, utc_now()),
            )

    def list_artifacts(self, session_id: str) -> list[Artifact]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT a.* FROM artifacts a
                JOIN session_artifacts sa ON sa.artifact_id = a.id
                WHERE sa.session_id = ? ORDER BY sa.attached_at
                """,
                (session_id,),
            ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        return self._row_to_artifact(row) if row else None

    def set_data(self, name: str, kind: str, value: str) -> None:
        if kind not in {"text", "artifact"}:
            raise ValueError("kind danych musi być 'text' albo 'artifact'")
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO data_items(name, kind, value, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    kind = excluded.kind, value = excluded.value, updated_at = excluded.updated_at
                """,
                (name, kind, value, now, now),
            )

    def get_data(self, name: str) -> tuple[str, str] | None:
        with self._connect() as db:
            row = db.execute("SELECT kind, value FROM data_items WHERE name = ?", (name,)).fetchone()
        return (str(row["kind"]), str(row["value"])) if row else None

    def list_data(self) -> list[tuple[str, str, str]]:
        with self._connect() as db:
            rows = db.execute("SELECT name, kind, value FROM data_items ORDER BY name").fetchall()
        return [(str(row["name"]), str(row["kind"]), str(row["value"])) for row in rows]

    def delete_data(self, name: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM data_items WHERE name = ?", (name,))
        return cursor.rowcount > 0

    def bind_secret(self, alias: str, secret_ref: str) -> None:
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO secret_bindings(alias, secret_ref, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    secret_ref = excluded.secret_ref, updated_at = excluded.updated_at
                """,
                (alias, secret_ref, now, now),
            )

    def get_secret_binding(self, alias: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT secret_ref FROM secret_bindings WHERE alias = ?", (alias,)
            ).fetchone()
        return str(row["secret_ref"]) if row else None

    def list_secret_bindings(self) -> list[tuple[str, str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT alias, secret_ref FROM secret_bindings ORDER BY alias"
            ).fetchall()
        return [(str(row["alias"]), str(row["secret_ref"])) for row in rows]

    def unbind_secret(self, alias: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM secret_bindings WHERE alias = ?", (alias,))
        return cursor.rowcount > 0

    # Compact conversation state ------------------------------------------
    def set_session_state(self, session_id: str, state: dict[str, Any]) -> None:
        now = utc_now()
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO session_state(session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json, updated_at = excluded.updated_at
                """,
                (session_id, encoded, now),
            )

    def get_session_state(self, session_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT state_json FROM session_state WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row["state_json"] or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    # Routing and usage ----------------------------------------------------
    def record_routing_decision(
        self,
        session_id: str,
        *,
        route: str,
        reason: str,
        intent_id: str = "",
        confidence: float = 0.0,
        provider: str = "",
        model: str = "",
        candidates: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO routing_decisions(
                    session_id, route, reason, intent_id, confidence, provider, model,
                    candidates_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    route,
                    reason,
                    intent_id,
                    float(confidence),
                    provider,
                    model,
                    json.dumps(candidates or [], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def last_routing_decision(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM routing_decisions WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self._routing_row(row) if row else None

    def list_routing_decisions(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM routing_decisions WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._routing_row(row) for row in rows]

    def record_provider_usage(
        self,
        session_id: str,
        *,
        provider: str,
        model: str,
        purpose: str,
        usage: TokenUsage,
        input_cost_per_million: float = 0.0,
        cached_input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> float:
        input_tokens = max(0, int(usage.input_tokens))
        cached = min(input_tokens, max(0, int(usage.cached_input_tokens)))
        uncached = input_tokens - cached
        cost = (
            uncached * float(input_cost_per_million)
            + cached * float(cached_input_cost_per_million)
            + max(0, int(usage.output_tokens)) * float(output_cost_per_million)
        ) / 1_000_000
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO provider_usage(
                    session_id, provider, model, purpose, input_tokens, cached_input_tokens,
                    output_tokens, estimated, cost_usd, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    provider,
                    model,
                    purpose,
                    input_tokens,
                    cached,
                    max(0, int(usage.output_tokens)),
                    1 if usage.estimated else 0,
                    cost,
                    json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
                    utc_now(),
                ),
            )
        return cost

    def usage_summary(self, session_id: str | None = None) -> dict[str, Any]:
        where = "WHERE session_id = ?" if session_id else ""
        params: tuple[Any, ...] = (session_id,) if session_id else ()
        with self._connect() as db:
            total = db.execute(
                f"""
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd,
                       COALESCE(SUM(estimated), 0) AS estimated_calls
                FROM provider_usage {where}
                """,
                params,
            ).fetchone()
            by_provider = db.execute(
                f"""
                SELECT provider, model, purpose, COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM provider_usage {where}
                GROUP BY provider, model, purpose
                ORDER BY cost_usd DESC, calls DESC
                """,
                params,
            ).fetchall()
            route_where = "WHERE session_id = ?" if session_id else ""
            route_rows = db.execute(
                f"SELECT route, COUNT(*) AS count FROM routing_decisions {route_where} GROUP BY route ORDER BY count DESC",
                params,
            ).fetchall()
        calls = int(total["calls"] or 0)
        routes = {str(row["route"]): int(row["count"]) for row in route_rows}
        llm_free = sum(routes.get(name, 0) for name in ("deterministic", "cache"))
        route_total = sum(routes.values())
        return {
            "scope": session_id or "all",
            "calls": calls,
            "input_tokens": int(total["input_tokens"] or 0),
            "cached_input_tokens": int(total["cached_input_tokens"] or 0),
            "output_tokens": int(total["output_tokens"] or 0),
            "estimated_calls": int(total["estimated_calls"] or 0),
            "cost_usd": round(float(total["cost_usd"] or 0.0), 8),
            "routes": routes,
            "llm_free_route_share": round(llm_free / route_total, 4) if route_total else 0.0,
            "by_provider": [
                {
                    "provider": str(row["provider"]),
                    "model": str(row["model"]),
                    "purpose": str(row["purpose"]),
                    "calls": int(row["calls"]),
                    "input_tokens": int(row["input_tokens"]),
                    "cached_input_tokens": int(row["cached_input_tokens"]),
                    "output_tokens": int(row["output_tokens"]),
                    "cost_usd": round(float(row["cost_usd"]), 8),
                }
                for row in by_provider
            ],
        }

    # Plans and receipts ---------------------------------------------------
    def save_execution_plan(self, plan: dict[str, Any]) -> None:
        now = utc_now()
        created_at = str(plan.get("created_at", now))
        encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO execution_plans(id, session_id, intent_id, status, effect, plan_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status, effect = excluded.effect,
                    plan_json = excluded.plan_json, updated_at = excluded.updated_at
                """,
                (
                    str(plan["id"]),
                    str(plan["session_id"]),
                    str(plan.get("intent_id", "")),
                    str(plan.get("status", "planned")),
                    str(plan.get("effect", "read")),
                    encoded,
                    created_at,
                    now,
                ),
            )

    def get_execution_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT plan_json, status FROM execution_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["plan_json"])
        payload["status"] = str(row["status"])
        return payload

    def list_execution_plans(
        self, session_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            if session_id:
                rows = db.execute(
                    "SELECT plan_json, status FROM execution_plans WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT plan_json, status FROM execution_plans ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["plan_json"])
            payload["status"] = str(row["status"])
            result.append(payload)
        return result

    def update_plan_status(self, plan_id: str, status: str) -> None:
        payload = self.get_execution_plan(plan_id)
        if not payload:
            raise KeyError(f"Nie ma planu {plan_id}")
        payload["status"] = status
        now = utc_now()
        with self._connect() as db:
            db.execute(
                "UPDATE execution_plans SET status = ?, plan_json = ?, updated_at = ? WHERE id = ?",
                (
                    status,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now,
                    plan_id,
                ),
            )

    def save_execution_receipt(self, receipt: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO execution_receipts(id, plan_id, session_id, ok, receipt_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(receipt["id"]),
                    str(receipt["plan_id"]),
                    str(receipt["session_id"]),
                    1 if receipt.get("ok") else 0,
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    str(receipt.get("created_at", utc_now())),
                ),
            )

    def get_execution_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT receipt_json FROM execution_receipts WHERE id = ?", (receipt_id,)
            ).fetchone()
        return json.loads(row["receipt_json"]) if row else None

    def list_execution_receipts(
        self, session_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            if session_id:
                rows = db.execute(
                    "SELECT receipt_json FROM execution_receipts WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT receipt_json FROM execution_receipts ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [json.loads(row["receipt_json"]) for row in rows]

    def record_router_feedback(
        self,
        session_id: str,
        *,
        intent_id: str,
        route: str,
        success: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO router_feedback(session_id, intent_id, route, success, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    intent_id,
                    route,
                    1 if success else 0,
                    json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
                    utc_now(),
                ),
            )

    def historical_success(self, intent_id: str, route: str = "") -> float | None:
        clause = "WHERE intent_id = ?"
        params: list[Any] = [intent_id]
        if route:
            clause += " AND route = ?"
            params.append(route)
        with self._connect() as db:
            row = db.execute(
                f"SELECT COUNT(*) AS n, AVG(success) AS rate FROM router_feedback {clause}",
                tuple(params),
            ).fetchone()
        if not row or int(row["n"] or 0) < 3:
            return None
        return float(row["rate"] or 0.0)

    # Safe IntentIR cache --------------------------------------------------
    def cache_get(self, key: str) -> dict[str, Any] | None:
        now = time.time()
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json, expires_at FROM context_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if row and float(row["expires_at"]) < now:
                db.execute("DELETE FROM context_cache WHERE cache_key = ?", (key,))
                row = None
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def cache_set(self, key: str, payload: dict[str, Any], ttl_seconds: int = 86_400) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO context_cache(cache_key, payload_json, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    key,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    time.time() + max(1, int(ttl_seconds)),
                    utc_now(),
                ),
            )

    # State fingerprint and export ----------------------------------------
    def state_fingerprint(self) -> str:
        with self._connect() as db:
            data = [tuple(row) for row in db.execute(
                "SELECT name, kind, value, updated_at FROM data_items ORDER BY name"
            ).fetchall()]
            bindings = [tuple(row) for row in db.execute(
                "SELECT alias, secret_ref, updated_at FROM secret_bindings ORDER BY alias"
            ).fetchall()]
            artifacts = [tuple(row) for row in db.execute(
                "SELECT id, size FROM artifacts ORDER BY id"
            ).fetchall()]
        encoded = json.dumps(
            {"data": data, "bindings": bindings, "artifacts": artifacts},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def export_session(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        if not session:
            raise KeyError(f"Nie ma sesji {session_id}")
        return {
            "session": {
                "id": session.id,
                "name": session.name,
                "provider": session.provider,
                "model": session.model,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
            },
            "working_state": self.get_session_state(session_id),
            "messages": [
                {
                    "id": msg.id,
                    "role": msg.role,
                    "content": msg.display_content,
                    "metadata": msg.metadata,
                    "created_at": msg.created_at,
                }
                for msg in self.list_messages(session_id)
            ],
            "artifacts": [
                {
                    "id": item.id,
                    "original_path": item.original_path,
                    "stored_path": str(item.stored_path),
                    "mime_type": item.mime_type,
                    "size": item.size,
                    "created_at": item.created_at,
                }
                for item in self.list_artifacts(session_id)
            ],
            "routing": self.list_routing_decisions(session_id),
            "plans": self.list_execution_plans(session_id),
            "receipts": self.list_execution_receipts(session_id),
            "usage": self.usage_summary(session_id),
        }

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            id=str(row["id"]),
            name=str(row["name"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        return Message(
            id=int(row["id"]),
            session_id=str(row["session_id"]),
            role=str(row["role"]),  # type: ignore[arg-type]
            display_content=str(row["display_content"]),
            context_content=str(row["context_content"]),
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=str(row["id"]),
            original_path=str(row["original_path"]),
            stored_path=Path(row["stored_path"]),
            mime_type=str(row["mime_type"]),
            size=int(row["size"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _routing_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "session_id": str(row["session_id"]),
            "route": str(row["route"]),
            "reason": str(row["reason"]),
            "intent_id": str(row["intent_id"]),
            "confidence": float(row["confidence"]),
            "provider": str(row["provider"]),
            "model": str(row["model"]),
            "candidates": json.loads(row["candidates_json"] or "[]"),
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "created_at": str(row["created_at"]),
        }

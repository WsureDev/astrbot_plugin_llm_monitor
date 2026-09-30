from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any


class EventStore:
    def __init__(self, path: Path, *, max_text_chars: int, retention_days: int, redact_secrets: bool):
        self.path = path
        self.max_text_chars = max_text_chars
        self.retention_days = retention_days
        self.redact_secrets = redact_secrets
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=2000)
        self.writer_task: asyncio.Task | None = None
        self.dropped = 0

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.writer_task = asyncio.create_task(self._writer())

    async def close(self) -> None:
        if self.writer_task is None:
            return
        await self.queue.put(("__stop__", {}))
        await self.writer_task
        self.writer_task = None

    def enqueue(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait((kind, payload))
        except asyncio.QueueFull:
            self.dropped += 1

    def _initialize(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    status TEXT NOT NULL,
                    umo TEXT,
                    platform_name TEXT,
                    platform_id TEXT,
                    message_type TEXT,
                    sender_id TEXT,
                    sender_name TEXT,
                    group_id TEXT,
                    conversation_id TEXT,
                    provider_id TEXT,
                    provider_model TEXT,
                    queued_at REAL,
                    duration REAL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    status TEXT NOT NULL,
                    provider_id TEXT,
                    provider_model TEXT,
                    duration REAL,
                    ttft REAL,
                    input_other INTEGER DEFAULT 0,
                    input_cached INTEGER DEFAULT 0,
                    output INTEGER DEFAULT 0,
                    attempt_kind TEXT DEFAULT 'round',
                    is_fallback INTEGER DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    status TEXT NOT NULL,
                    duration REAL,
                    tool_name TEXT,
                    input_json TEXT,
                    output_json TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_tasks_created_at ON tasks(created_at DESC);
                CREATE INDEX IF NOT EXISTS ix_llm_calls_task ON llm_calls(task_id, sequence);
                CREATE INDEX IF NOT EXISTS ix_tool_calls_task ON tool_calls(task_id, sequence);
                """
            )
            try:
                db.execute("ALTER TABLE tasks ADD COLUMN duration REAL")
            except sqlite3.OperationalError:
                pass
            try:
                db.execute("ALTER TABLE tool_calls ADD COLUMN duration REAL")
            except sqlite3.OperationalError:
                pass
            for statement in (
                "ALTER TABLE llm_calls ADD COLUMN attempt_kind TEXT DEFAULT 'round'",
                "ALTER TABLE llm_calls ADD COLUMN is_fallback INTEGER DEFAULT 0",
            ):
                try:
                    db.execute(statement)
                except sqlite3.OperationalError:
                    pass
            cutoff = time.time() - max(1, self.retention_days) * 86400
            db.execute("DELETE FROM tasks WHERE created_at < ?", (cutoff,))
            db.execute("DELETE FROM llm_calls WHERE started_at < ?", (cutoff,))
            db.execute("DELETE FROM tool_calls WHERE started_at < ?", (cutoff,))

    async def _writer(self) -> None:
        while True:
            kind, payload = await self.queue.get()
            try:
                if kind == "__stop__":
                    return
                self._apply_event(kind, payload)
            except Exception:
                # A broken monitoring record must not affect the main event loop.
                pass
            finally:
                self.queue.task_done()

    def _apply_event(self, kind: str, payload: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as db:
            if kind == "task_start":
                db.execute(
                    """INSERT OR IGNORE INTO tasks
                    (task_id, created_at, status, umo, platform_name, platform_id,
                     message_type, sender_id, sender_name, group_id, conversation_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload.get("task_id"),
                        payload.get("created_at", time.time()),
                        payload.get("status", "running"),
                        payload.get("umo", ""),
                        payload.get("platform_name", ""),
                        payload.get("platform_id", ""),
                        payload.get("message_type", ""),
                        payload.get("sender_id", ""),
                        payload.get("sender_name", ""),
                        payload.get("group_id", ""),
                        payload.get("conversation_id", ""),
                    ),
                )
            elif kind == "task_update":
                db.execute(
                    """UPDATE tasks SET conversation_id = COALESCE(NULLIF(?, ''), conversation_id),
                    provider_id = ?, provider_model = ?, started_at = ?, queued_at = ?
                    WHERE task_id = ?""",
                    (
                        payload.get("conversation_id", ""),
                        payload.get("provider_id", ""),
                        payload.get("provider_model", ""),
                        payload.get("started_at"),
                        payload.get("queued_at"),
                        payload.get("task_id"),
                    ),
                )
            elif kind == "task_end":
                db.execute(
                    """UPDATE tasks SET finished_at = ?, status = ?,
                    duration = MAX(0, ? - COALESCE(started_at, created_at))
                    WHERE task_id = ?""",
                    (
                        payload.get("finished_at"),
                        payload.get("status", "completed"),
                        payload.get("finished_at", time.time()),
                        payload.get("task_id"),
                    ),
                )
            elif kind == "llm_start":
                db.execute(
                    """INSERT OR REPLACE INTO llm_calls
                    (id, task_id, sequence, started_at, status, provider_id, provider_model,
                     attempt_kind, is_fallback)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload.get("id"),
                        payload.get("task_id"),
                        payload.get("sequence", 0),
                        payload.get("started_at", time.time()),
                        "running",
                        payload.get("provider_id", ""),
                        payload.get("provider_model", ""),
                        payload.get("attempt_kind", "round"),
                        int(bool(payload.get("is_fallback", False))),
                    ),
                )
            elif kind == "llm_end":
                usage = payload.get("usage") or {}
                db.execute(
                    """UPDATE llm_calls SET finished_at = ?, status = ?, duration = ?, ttft = ?,
                    provider_id = ?, provider_model = ?, input_other = ?, input_cached = ?,
                    output = ?, attempt_kind = ?, is_fallback = ?, error = ? WHERE id = ?""",
                    (
                        payload.get("finished_at"),
                        payload.get("status", "completed"),
                        payload.get("duration", 0.0),
                        payload.get("ttft", 0.0),
                        payload.get("provider_id", ""),
                        payload.get("provider_model", ""),
                        int(usage.get("input_other", 0) or 0),
                        int(usage.get("input_cached", 0) or 0),
                        int(usage.get("output", 0) or 0),
                        payload.get("attempt_kind", "round"),
                        int(bool(payload.get("is_fallback", False))),
                        payload.get("error", ""),
                        payload.get("id"),
                    ),
                )
            elif kind == "tool_start":
                db.execute(
                    """INSERT OR REPLACE INTO tool_calls
                    (id, task_id, sequence, started_at, status, tool_name, input_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload.get("id"),
                        payload.get("task_id"),
                        payload.get("sequence", 0),
                        payload.get("started_at", time.time()),
                        "running",
                        payload.get("tool_name", ""),
                        payload.get("input_json", ""),
                    ),
                )
            elif kind == "tool_end":
                db.execute(
                    """UPDATE tool_calls SET finished_at = ?, status = ?,
                    duration = MAX(0, ? - started_at), output_json = ?, error = ?
                    WHERE id = ?""",
                    (
                        payload.get("finished_at"),
                        payload.get("status", "completed"),
                        payload.get("finished_at", time.time()),
                        payload.get("output_json", ""),
                        payload.get("error", ""),
                        payload.get("id"),
                    ),
                )

    async def list_tasks(
        self,
        limit: int,
        offset: int,
        status: str = "",
        model: str = "",
        platform: str = "",
    ) -> dict[str, Any]:
        return self._list_tasks_sync(limit, offset, status, model, platform)

    def _list_tasks_sync(
        self,
        limit: int,
        offset: int,
        status: str,
        model: str,
        platform: str,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        args: list[Any] = []
        if status:
            clauses.append("status = ?")
            args.append(status)
        if model:
            clauses.append("provider_model LIKE ?")
            args.append(f"%{model}%")
        if platform:
            clauses.append("platform_name = ?")
            args.append(platform)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                f"""SELECT tasks.*,
                (SELECT COUNT(*) FROM llm_calls WHERE llm_calls.task_id = tasks.task_id) AS llm_call_count,
                (SELECT COUNT(*) FROM tool_calls WHERE tool_calls.task_id = tasks.task_id) AS tool_call_count
                FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                [*args, limit, offset],
            ).fetchall()
            return {"items": [dict(row) for row in rows], "limit": limit, "offset": offset}

    async def summary(self, hours: int) -> dict[str, Any]:
        cutoff = time.time() - hours * 3600
        with sqlite3.connect(self.path) as db:
            task_count, running_count = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(status = 'running'), 0) FROM tasks WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()
            calls, input_other, input_cached, output, failed_calls, fallback_calls = db.execute(
                """SELECT COUNT(*), COALESCE(SUM(input_other), 0),
                COALESCE(SUM(input_cached), 0), COALESCE(SUM(output), 0),
                COALESCE(SUM(status = 'error'), 0), COALESCE(SUM(is_fallback), 0)
                FROM llm_calls WHERE started_at >= ?""",
                (cutoff,),
            ).fetchone()
        return {
            "hours": hours,
            "tasks": int(task_count or 0),
            "running_tasks": int(running_count or 0),
            "llm_calls": int(calls or 0),
            "input_other": int(input_other or 0),
            "input_cached": int(input_cached or 0),
            "output": int(output or 0),
            "failed_calls": int(failed_calls or 0),
            "fallback_calls": int(fallback_calls or 0),
            "total_tokens": int(input_other or 0) + int(input_cached or 0) + int(output or 0),
        }

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self._get_task_sync(task_id)

    def _get_task_sync(self, task_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            task = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                return None
            calls = db.execute("SELECT * FROM llm_calls WHERE task_id = ? ORDER BY sequence", (task_id,)).fetchall()
            tools = db.execute("SELECT * FROM tool_calls WHERE task_id = ? ORDER BY sequence", (task_id,)).fetchall()
            return {"task": dict(task), "llm_calls": [dict(row) for row in calls], "tool_calls": [dict(row) for row in tools]}

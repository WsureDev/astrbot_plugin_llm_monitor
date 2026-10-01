"""SQLite persistence: one writer thread, bounded queue, independent read workers."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from .serialization import redact_text, safe_json

logger = logging.getLogger("astrbot_plugin_llm_monitor")

RETRY_COLUMNS = {
    "round_id": "TEXT",
    "retry_group_id": "TEXT",
    "attempt_number": "INTEGER DEFAULT 1",
    "is_retry": "INTEGER DEFAULT 0",
    "retry_reason": "TEXT",
    "retry_wait": "REAL DEFAULT 0",
    "retry_wait_planned": "REAL",
    "wait_kind": "TEXT",
    "next_retry_wait": "REAL",
    "next_retry_wait_actual": "REAL",
    "retry_wait_status": "TEXT",
}


class EventStore:
    def __init__(
        self,
        path: Path,
        *,
        max_text_chars=20000,
        retention_days=30,
        redact_secrets=True,
        queue_size=2000,
        cleanup_interval=3600,
    ):
        self.path = Path(path)
        self.max_text_chars = max(1000, min(int(max_text_chars or 20000), 100000))
        self.retention_days = max(1, min(int(retention_days or 30), 365))
        self.redact_secrets = bool(redact_secrets)
        self.cleanup_interval = max(1, cleanup_interval)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.writer_task: asyncio.Task | None = None
        self._writer_pool = None
        self._reader_pool = None
        self._db = None
        self._accepting = False
        self._lock = threading.Lock()
        self._stats = dict(
            accepted=0,
            persisted=0,
            dropped=0,
            failed=0,
            last_success_at=None,
            last_error_at=None,
            last_error="",
            last_cleanup_at=None,
        )
        self._last_warning = 0.0
        self._next_cleanup = 0.0
        self._init_error = ""

    @property
    def accepting(self):
        return self._accepting and self.writer_task is not None and not self.writer_task.done()

    @property
    def dropped(self):
        return self.health()["dropped"]

    def health(self):
        with self._lock:
            result = dict(self._stats)
        result.update(
            accepting=self.accepting,
            queue_depth=self.queue.qsize(),
            queue_capacity=self.queue.maxsize,
            initialized=self._db is not None,
        )
        result["ok"] = (
            self.accepting
            and not self._init_error
            and not result["last_error"]
            and not result["dropped"]
        )
        if self._init_error:
            result["last_error"] = self._init_error
        return result

    def _failure(self, count: int, exc: Exception):
        message = redact_text(f"{type(exc).__name__}: {exc}", 2000, self.redact_secrets)
        now = time.time()
        with self._lock:
            self._stats["failed"] += count
            self._stats.update(last_error_at=now, last_error=message)
        if time.monotonic() - self._last_warning >= 30:
            logger.warning("Monitor persistence degraded: %s", message)
            self._last_warning = time.monotonic()

    async def start(self):
        if self.accepting:
            return
        self._writer_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="llm-monitor-writer"
        )
        self._reader_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="llm-monitor-reader"
        )
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._writer_pool, self._initialize_sync
            )
        except Exception as exc:
            self._init_error = redact_text(str(exc), 2000, self.redact_secrets)
            self._failure(0, exc)
            await self.close()
            raise
        self._init_error = ""
        self._accepting = True
        self.writer_task = asyncio.create_task(self._writer(), name="llm-monitor-writer")

    def enqueue(self, kind: str, payload: dict) -> bool:
        if not self.accepting:
            return False
        try:
            self.queue.put_nowait((kind, payload))
        except asyncio.QueueFull:
            with self._lock:
                self._stats["dropped"] += 1
            return False
        with self._lock:
            self._stats["accepted"] += 1
        return True

    async def flush(self):
        await self.queue.join()

    async def close(self):
        self._accepting = False
        if self.writer_task is not None:
            if not self.writer_task.done():
                await self.queue.put(None)
            await self.writer_task
            self.writer_task = None
        if self._writer_pool is not None:
            await asyncio.get_running_loop().run_in_executor(self._writer_pool, self._close_sync)
            pool, self._writer_pool = self._writer_pool, None
            await asyncio.to_thread(pool.shutdown, wait=True)
        if self._reader_pool is not None:
            pool, self._reader_pool = self._reader_pool, None
            await asyncio.to_thread(pool.shutdown, wait=True)

    def _close_sync(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def _initialize_sync(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=0.5)
        db = self._db
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=500")
        if db.execute("PRAGMA user_version").fetchone()[0] > 4:
            raise RuntimeError("Database schema is newer than this plugin")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, created_at REAL NOT NULL, started_at REAL,
                finished_at REAL, status TEXT NOT NULL, umo TEXT, platform_name TEXT,
                platform_id TEXT, message_type TEXT, sender_id TEXT, sender_name TEXT,
                group_id TEXT, conversation_id TEXT, provider_id TEXT, provider_model TEXT,
                queued_at REAL, duration REAL, error TEXT, parent_task_id TEXT,
                trigger TEXT, external_task_id TEXT, end_inferred INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS llm_calls (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                provider_id TEXT, provider_model TEXT, duration REAL, ttft REAL,
                input_other INTEGER DEFAULT 0, input_cached INTEGER DEFAULT 0,
                output INTEGER DEFAULT 0, attempt_kind TEXT DEFAULT 'round',
                is_fallback INTEGER DEFAULT 0, error TEXT, response_model TEXT,
                input_text TEXT, output_text TEXT,
                end_inferred INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tool_calls (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                duration REAL, tool_name TEXT, input_json TEXT, output_json TEXT,
                error TEXT, end_inferred INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS retry_attempts (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, llm_call_id TEXT NOT NULL, parent_id TEXT,
                layer TEXT NOT NULL, sequence INTEGER NOT NULL, operation TEXT,
                started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                duration REAL, error TEXT, http_status INTEGER, end_inferred INTEGER DEFAULT 0);
        """)
        migrations = {
            "tasks": {
                "duration": "REAL",
                "parent_task_id": "TEXT",
                "trigger": "TEXT",
                "external_task_id": "TEXT",
                "end_inferred": "INTEGER DEFAULT 0",
            },
            "retry_attempts": dict(RETRY_COLUMNS),
            "llm_calls": {
                **RETRY_COLUMNS,
                "attempt_kind": "TEXT DEFAULT 'round'",
                "is_fallback": "INTEGER DEFAULT 0",
                "response_model": "TEXT",
                "input_text": "TEXT",
                "output_text": "TEXT",
                "end_inferred": "INTEGER DEFAULT 0",
            },
            "tool_calls": {"duration": "REAL", "end_inferred": "INTEGER DEFAULT 0"},
        }
        with db:
            for table, fields in migrations.items():
                existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for field, definition in fields.items():
                    if field not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {field} {definition}")
            for statement in (
                "CREATE INDEX IF NOT EXISTS ix_tasks_created_at ON tasks(created_at DESC)",
                "CREATE INDEX IF NOT EXISTS ix_tasks_status_time ON tasks(status, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_llm_calls_task ON llm_calls(task_id, sequence)",
                "CREATE INDEX IF NOT EXISTS ix_llm_calls_time ON llm_calls(started_at)",
                "CREATE INDEX IF NOT EXISTS ix_retry_attempts_task ON retry_attempts(task_id, started_at)",
                "CREATE INDEX IF NOT EXISTS ix_retry_attempts_call ON retry_attempts(llm_call_id, layer)",
                "CREATE INDEX IF NOT EXISTS ix_tool_calls_task ON tool_calls(task_id, sequence)",
            ):
                db.execute(statement)
            db.execute("PRAGMA user_version=4")
        self._cleanup_sync()

    def _cleanup_sync(self):
        cutoff = time.time() - self.retention_days * 86400
        with self._db as db:
            # Retain complete task trees and never remove active work.
            expired = "SELECT task_id FROM tasks WHERE created_at < ? AND status != 'running'"
            for table in ("llm_calls", "tool_calls", "retry_attempts"):
                db.execute(f"DELETE FROM {table} WHERE task_id IN ({expired})", (cutoff,))
                db.execute(
                    f"DELETE FROM {table} WHERE started_at < ? AND NOT EXISTS "
                    f"(SELECT 1 FROM tasks WHERE tasks.task_id={table}.task_id)",
                    (cutoff,),
                )
            db.execute("DELETE FROM tasks WHERE created_at < ? AND status != 'running'", (cutoff,))
        self._next_cleanup = time.monotonic() + self.cleanup_interval
        with self._lock:
            self._stats["last_cleanup_at"] = time.time()

    async def _writer(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                first = await asyncio.wait_for(
                    self.queue.get(), timeout=max(0.01, self._next_cleanup - time.monotonic())
                )
            except asyncio.TimeoutError:
                try:
                    await loop.run_in_executor(self._writer_pool, self._cleanup_sync)
                except Exception as exc:
                    self._failure(0, exc)
                    self._next_cleanup = time.monotonic() + 60
                continue
            if first is None:
                self.queue.task_done()
                return
            batch = [first]
            stop = False
            while len(batch) < 100:
                try:
                    item = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    self.queue.task_done()
                    stop = True
                    break
                batch.append(item)
            try:
                await loop.run_in_executor(self._writer_pool, self._apply_batch_sync, batch)
            finally:
                for _ in batch:
                    self.queue.task_done()
            if stop:
                return
            if time.monotonic() >= self._next_cleanup:
                try:
                    await loop.run_in_executor(self._writer_pool, self._cleanup_sync)
                except Exception as exc:
                    self._failure(0, exc)
                    self._next_cleanup = time.monotonic() + 60

    def _apply_batch_sync(self, batch):
        failures = []
        successful = 0
        try:
            with self._db as db:
                db.execute("BEGIN")
                for kind, payload in batch:
                    db.execute("SAVEPOINT monitor_event")
                    try:
                        self._apply_event(db, kind, payload)
                    except sqlite3.OperationalError:
                        # A busy/damaged database must not wait once per queued event.
                        raise
                    except Exception as exc:
                        db.execute("ROLLBACK TO monitor_event")
                        failures.append(exc)
                    else:
                        successful += 1
                    finally:
                        db.execute("RELEASE monitor_event")
        except Exception as exc:
            self._failure(len(batch), exc)
            return
        for exc in failures:
            self._failure(1, exc)
        if successful:
            with self._lock:
                self._stats["persisted"] += successful
                self._stats["last_success_at"] = time.time()

    def _apply_event(self, db, kind, p):
        error = redact_text(str(p.get("error") or ""), self.max_text_chars, self.redact_secrets)
        if kind == "task_start":
            db.execute(
                """INSERT OR IGNORE INTO tasks
                (task_id,created_at,status,umo,platform_name,platform_id,message_type,sender_id,sender_name,group_id,conversation_id,parent_task_id,trigger,external_task_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p["task_id"],
                    p["created_at"],
                    "running",
                    p.get("umo", ""),
                    p.get("platform_name", ""),
                    p.get("platform_id", ""),
                    p.get("message_type", ""),
                    p.get("sender_id", ""),
                    p.get("sender_name", ""),
                    p.get("group_id", ""),
                    p.get("conversation_id", ""),
                    p.get("parent_task_id"),
                    p.get("trigger", ""),
                    p.get("external_task_id", ""),
                ),
            )
            return
        if kind == "task_update":
            cursor = db.execute(
                """UPDATE tasks SET conversation_id=COALESCE(NULLIF(?,''),conversation_id),
                provider_id=?,provider_model=?,started_at=COALESCE(started_at,?),queued_at=? WHERE task_id=?""",
                (
                    p.get("conversation_id", ""),
                    p.get("provider_id", ""),
                    p.get("provider_model", ""),
                    p.get("started_at"),
                    p.get("queued_at"),
                    p["task_id"],
                ),
            )
        elif kind in {"task_end", "task_recover"}:
            recovered = kind == "task_recover"
            status = "recovered" if recovered else p["status"]
            finish = p["finished_at"]
            reason = redact_text(
                str(p.get("reason") or error), self.max_text_chars, self.redact_secrets
            )
            cursor = db.execute(
                """UPDATE tasks SET finished_at=?,status=?,duration=MAX(0,?-COALESCE(started_at,created_at)),
                error=?,end_inferred=? WHERE task_id=?"""
                + (" AND status='running'" if recovered else ""),
                (finish, status, finish, reason, int(recovered), p["task_id"]),
            )
            child_status = "interrupted" if status == "completed" else status
            for table in ("llm_calls", "tool_calls", "retry_attempts"):
                db.execute(
                    f"""UPDATE {table} SET status=?,finished_at=?,duration=MAX(0,?-started_at),
                    end_inferred=1,error=COALESCE(NULLIF(error,''),?) WHERE task_id=? AND status='running'""",
                    (
                        child_status,
                        finish,
                        finish,
                        reason or "Parent task ended without a final call record",
                        p["task_id"],
                    ),
                )
            if recovered:
                return
        elif kind == "llm_start":
            db.execute(
                """INSERT INTO llm_calls
                (id,task_id,sequence,started_at,status,provider_id,provider_model,attempt_kind,is_fallback)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    p["id"],
                    p["task_id"],
                    p["sequence"],
                    p["started_at"],
                    "running",
                    p.get("provider_id", ""),
                    p.get("provider_model", ""),
                    p.get("attempt_kind", "round"),
                    int(p.get("is_fallback", False)),
                ),
            )
            self._write_retry_fields(db, "llm_calls", p)
            self._write_text_fields(db, "llm_calls", p)
            return
        elif kind == "llm_end":
            usage = p.get("usage") or {}
            cursor = db.execute(
                """UPDATE llm_calls SET finished_at=?,status=?,duration=?,ttft=?,input_other=?,input_cached=?,
                output=?,error=?,response_model=?,end_inferred=0 WHERE id=?""",
                (
                    p["finished_at"],
                    p["status"],
                    p["duration"],
                    p.get("ttft"),
                    int(usage.get("input_other", 0) or 0),
                    int(usage.get("input_cached", 0) or 0),
                    int(usage.get("output", 0) or 0),
                    error,
                    p.get("response_model", ""),
                    p["id"],
                ),
            )
            self._write_text_fields(db, "llm_calls", p)
        elif kind == "attempt_start":
            db.execute(
                """INSERT INTO retry_attempts
                (id,task_id,llm_call_id,parent_id,layer,sequence,operation,started_at,status)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    p["id"],
                    p["task_id"],
                    p["llm_call_id"],
                    p.get("parent_id"),
                    p["layer"],
                    p["sequence"],
                    p.get("operation", ""),
                    p["started_at"],
                    "running",
                ),
            )
            self._write_retry_fields(db, "retry_attempts", p)
            return
        elif kind == "attempt_end":
            cursor = db.execute(
                """UPDATE retry_attempts SET finished_at=?,status=?,duration=?,error=?,
                http_status=?,end_inferred=0 WHERE id=?""",
                (
                    p["finished_at"],
                    p["status"],
                    p["duration"],
                    error,
                    p.get("http_status"),
                    p["id"],
                ),
            )
        elif kind == "retry_wait":
            table = "llm_calls" if p["layer"] == "framework" else "retry_attempts"
            cursor = db.execute(
                f"UPDATE {table} SET next_retry_wait=?, next_retry_wait_actual=?, retry_wait_status=? WHERE id=?",
                (p.get("planned"), p.get("actual"), p["status"], p["id"]),
            )
        elif kind == "tool_start":
            db.execute(
                """INSERT INTO tool_calls (id,task_id,sequence,started_at,status,tool_name,input_json)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    p["id"],
                    p["task_id"],
                    p["sequence"],
                    p["started_at"],
                    "running",
                    p["tool_name"],
                    safe_json(p.get("input"), self.max_text_chars, self.redact_secrets),
                ),
            )
            return
        elif kind == "tool_end":
            cursor = db.execute(
                """UPDATE tool_calls SET finished_at=?,status=?,duration=?,output_json=?,error=?,end_inferred=0
                WHERE id=?""",
                (
                    p["finished_at"],
                    p["status"],
                    p["duration"],
                    safe_json(p.get("output"), self.max_text_chars, self.redact_secrets),
                    error,
                    p["id"],
                ),
            )
        else:
            raise ValueError(f"Unknown monitor event: {kind}")
        if cursor.rowcount == 0:
            raise LookupError(f"{kind} has no matching start record")

    def _write_retry_fields(self, db, table, payload):
        fields = {key: payload[key] for key in RETRY_COLUMNS if key in payload}
        if "retry_reason" in fields:
            fields["retry_reason"] = redact_text(
                str(fields["retry_reason"] or ""), self.max_text_chars, self.redact_secrets
            )
        if fields:
            columns = ",".join(key + "=?" for key in fields)
            db.execute(
                f"UPDATE {table} SET {columns} WHERE id=?", (*fields.values(), payload["id"])
            )

    def _write_text_fields(self, db, table, payload):
        fields = {}
        for key in ("input_text", "output_text"):
            if key not in payload:
                continue
            value = payload[key]
            if key == "input_text" and not isinstance(value, str):
                value = safe_json(value, self.max_text_chars, self.redact_secrets)
            else:
                value = redact_text(str(value or ""), self.max_text_chars, self.redact_secrets)
            fields[key] = value
        if fields:
            columns = ",".join(key + "=?" for key in fields)
            db.execute(
                f"UPDATE {table} SET {columns} WHERE id=?", (*fields.values(), payload["id"])
            )

    def enqueue_recovered(self, task_id, reason):
        return self.enqueue(
            "task_recover", dict(task_id=task_id, reason=reason, finished_at=time.time())
        )

    async def _read(self, method, *args):
        if self._reader_pool is None or self._db is None:
            raise RuntimeError("Monitor storage is unavailable")
        return await asyncio.get_running_loop().run_in_executor(self._reader_pool, method, *args)

    def _connect_read(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _where(status="", model="", platform="", hours=24):
        parts, args = [], []
        if hours:
            parts.append("t.created_at>=?")
            args.append(time.time() - hours * 3600)
        if status:
            parts.append("t.status=?")
            args.append(status)
        platforms = (
            platform if isinstance(platform, (list, tuple)) else [platform] if platform else []
        )
        if platforms:
            parts.append("t.platform_name IN (" + ",".join("?" for _ in platforms) + ")")
            args.extend(platforms)
        models = model if isinstance(model, (list, tuple)) else [model] if model else []
        if models:
            clauses = []
            for value in models:
                value = str(value)
                pattern = (
                    "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
                clauses.append(r"""(t.provider_model LIKE ? ESCAPE '\' OR EXISTS (SELECT 1 FROM llm_calls c
                    WHERE c.task_id=t.task_id AND (c.provider_model LIKE ? ESCAPE '\' OR c.response_model LIKE ? ESCAPE '\')))""")
                args.extend([pattern, pattern, pattern])
            parts.append("(" + " OR ".join(clauses) + ")")
        return (" WHERE " + " AND ".join(parts) if parts else ""), args

    async def list_tasks(self, limit=50, offset=0, status="", model="", platform="", hours=24):
        return await self._read(
            self._list_tasks_sync, limit, offset, status, model, platform, hours
        )

    def _list_tasks_sync(self, limit, offset, status, model, platform, hours):
        where, args = self._where(status, model, platform, hours)
        with closing(self._connect_read()) as db:
            total = db.execute("SELECT COUNT(*) FROM tasks t" + where, args).fetchone()[0]
            rows = db.execute(
                """SELECT t.*,
                (SELECT COUNT(*) FROM llm_calls c WHERE c.task_id=t.task_id) llm_call_count,
                (SELECT COUNT(*) FROM tool_calls c WHERE c.task_id=t.task_id) tool_call_count
                FROM tasks t"""
                + where
                + " ORDER BY t.created_at DESC,t.task_id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return dict(
            items=[dict(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(rows) < total,
        )

    async def list_running_tasks(self):
        return await self._read(self._running_sync)

    def _running_sync(self):
        with closing(self._connect_read()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM tasks WHERE status='running' ORDER BY created_at"
                )
            ]

    async def filter_options(self, hours=24):
        return await self._read(self._filter_options_sync, hours)

    def _filter_options_sync(self, hours):
        where, args = self._where(hours=hours)
        conjunction = " AND " if where else " WHERE "
        with closing(self._connect_read()) as db:
            platforms = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT t.platform_name FROM tasks t"
                    + where
                    + conjunction
                    + "t.platform_name != '' ORDER BY t.platform_name",
                    args,
                ).fetchall()
            ]
            models = set()
            for column in ("provider_model", "response_model"):
                source = "c"
                query = (
                    "SELECT DISTINCT "
                    + source
                    + "."
                    + column
                    + " FROM llm_calls c JOIN tasks t ON t.task_id=c.task_id"
                    + where
                    + conjunction
                    + source
                    + "."
                    + column
                    + " != ''"
                )
                models.update(row[0] for row in db.execute(query, args).fetchall())
            models.update(
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT t.provider_model FROM tasks t"
                    + where
                    + conjunction
                    + "t.provider_model != ''",
                    args,
                ).fetchall()
            )
            models = sorted(models)
        return dict(models=models, platforms=platforms)

    async def get_task(self, task_id):
        return await self._read(self._get_task_sync, task_id)

    async def latest_task_identity(self, umo):
        return await self._read(self._latest_task_identity_sync, umo)

    def _latest_task_identity_sync(self, umo):
        with closing(self._connect_read()) as db:
            row = db.execute(
                """SELECT platform_name,platform_id,message_type,sender_id,sender_name,group_id
                FROM tasks WHERE umo=? ORDER BY created_at DESC LIMIT 1""",
                (umo,),
            ).fetchone()
            return dict(row) if row else None

    def _get_task_sync(self, task_id):
        with closing(self._connect_read()) as db:
            task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                return None
            return dict(
                task=dict(task),
                llm_calls=[
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM llm_calls WHERE task_id=? ORDER BY sequence,id", (task_id,)
                    )
                ],
                retry_attempts=[
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM retry_attempts WHERE task_id=? ORDER BY started_at,sequence,id",
                        (task_id,),
                    )
                ],
                tool_calls=[
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM tool_calls WHERE task_id=? ORDER BY sequence,id", (task_id,)
                    )
                ],
            )

    async def summary(self, hours=24, status="", model="", platform=""):
        return await self._read(self._summary_sync, hours, status, model, platform)

    def _summary_sync(self, hours, status, model, platform):
        where, args = self._where(status, model, platform, hours)
        with closing(self._connect_read()) as db:
            task_row = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(t.status='running'),0) FROM tasks t" + where, args
            ).fetchone()
            selected = "SELECT c.* FROM llm_calls c JOIN tasks t ON t.task_id=c.task_id" + where
            rows = db.execute(
                """WITH selected AS ("""
                + selected
                + """),
                aggregate AS (SELECT provider_id,provider_model,COUNT(*) calls,
                    SUM(status='running') running,SUM(status='error') errors,SUM(is_fallback) fallback_calls,
                    SUM(is_retry) retry_calls,
                    SUM(input_other) input_other,SUM(input_cached) input_cached,SUM(output) output,
                    AVG(CASE WHEN end_inferred=0 THEN ttft END) avg_ttft
                    FROM selected GROUP BY provider_id,provider_model),
                ranked AS (SELECT provider_id,provider_model,duration,
                    ROW_NUMBER() OVER(PARTITION BY provider_id,provider_model ORDER BY duration) rn,
                    COUNT(*) OVER(PARTITION BY provider_id,provider_model) cnt
                    FROM selected WHERE finished_at IS NOT NULL AND end_inferred=0 AND status IN ('completed','error')),
                percentile AS (SELECT provider_id,provider_model,
                    MAX(CASE WHEN rn=(cnt+1)/2 THEN duration END) p50,
                    MAX(CASE WHEN rn=(95*cnt+99)/100 THEN duration END) p95
                    FROM ranked GROUP BY provider_id,provider_model)
                SELECT a.*,p.p50,p.p95 FROM aggregate a LEFT JOIN percentile p
                ON a.provider_id=p.provider_id AND a.provider_model=p.provider_model ORDER BY calls DESC""",
                args,
            ).fetchall()
            attempt_rows = db.execute(
                """SELECT c.provider_id,c.provider_model,a.layer,
                COUNT(*) attempts,SUM(a.is_retry) retries FROM retry_attempts a
                JOIN llm_calls c ON c.id=a.llm_call_id JOIN tasks t ON t.task_id=c.task_id"""
                + where
                + " GROUP BY c.provider_id,c.provider_model,a.layer",
                args,
            ).fetchall()
        attempts = {(r["provider_id"], r["provider_model"], r["layer"]): r for r in attempt_rows}
        models = [dict(row) for row in rows]
        for model in models:
            for layer in ("provider", "request", "http"):
                row = attempts.get((model["provider_id"], model["provider_model"], layer), {})
                model[layer + "_retries"] = row["retries"] if row else 0
                model[layer + "_attempts"] = row["attempts"] if row else 0
        totals = {
            field: sum(row[field] or 0 for row in models)
            for field in (
                "calls",
                "running",
                "errors",
                "fallback_calls",
                "retry_calls",
                "provider_retries",
                "request_retries",
                "http_retries",
                "provider_attempts",
                "request_attempts",
                "http_attempts",
                "input_other",
                "input_cached",
                "output",
            )
        }
        return dict(
            hours=hours,
            tasks=task_row[0],
            running_tasks=task_row[1],
            llm_calls=totals["calls"],
            active_llm_calls=totals["running"],
            failed_calls=totals["errors"],
            fallback_calls=totals["fallback_calls"],
            retry_calls=totals["retry_calls"],
            provider_retries=totals["provider_retries"],
            request_retries=totals["request_retries"],
            http_retries=totals["http_retries"],
            provider_attempts=totals["provider_attempts"],
            request_attempts=totals["request_attempts"],
            http_attempts=totals["http_attempts"],
            input_other=totals["input_other"],
            input_cached=totals["input_cached"],
            output=totals["output"],
            total_tokens=totals["input_other"] + totals["input_cached"] + totals["output"],
            models=models,
        )

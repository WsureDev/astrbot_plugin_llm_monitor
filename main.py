"""AstrBot hooks and authenticated WebUI API for LLM telemetry."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import suppress
from pathlib import Path

from astrbot import __version__
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import json_response, request

from .continuation import CONTINUATION_EXTRA, IDENTITY_EXTRA, ContinuationProbe
from .probe import RunnerProbe, event_from, field
from .reply_filter import strip_thinking
from .serialization import redact_text
from .store import EventStore

PLUGIN_NAME = "astrbot_plugin_llm_monitor"
VERSION = "0.4.1"
TASK_EXTRA = f"{PLUGIN_NAME}.task_id"
TASK_STATE_EXTRA = f"{PLUGIN_NAME}.task_state"
QUEUED_EXTRA = f"{PLUGIN_NAME}.queued_at"
CALLER_EXTRA = IDENTITY_EXTRA
RECONCILE_INTERVAL_SECONDS = 60
RECONCILE_STARTUP_DELAY_SECONDS = 5
RECONCILE_GRACE_SECONDS = 15
VALID_STATUSES = {
    "",
    "running",
    "completed",
    "error",
    "aborted",
    "cancelled",
    "interrupted",
    "recovered",
}


class LLMMonitorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context, self.config = context, config
        self.store = EventStore(
            Path(StarTools.get_data_dir(PLUGIN_NAME)) / "events.sqlite3",
            max_text_chars=self._cfg("max_text_chars", 20000),
            retention_days=self._cfg("retention_days", 30),
            redact_secrets=self._cfg("redact_secrets", True),
        )
        self.probe = RunnerProbe(self)
        self._started = False
        self._reconcile_task = None
        self._record_errors = 0
        self._last_record_error = ""
        self._last_warning = 0.0
        self._recovery_error = ""
        self.continuations = ContinuationProbe(self)
        for route, handler in (
            ("health", self.health),
            ("self-check", self.self_check_api),
            ("summary", self.summary_api),
            ("filters", self.filters_api),
            ("tasks", self.tasks_api),
            ("tasks/<task_id>", self.task_api),
        ):
            context.register_web_api(
                f"/{PLUGIN_NAME}/{route}", handler, ["GET"], "LLM monitor " + route
            )

    def _cfg(self, key, default):
        section = self.config.get("monitor", {})
        return section.get(key, default) if isinstance(section, dict) else default

    def capture_enabled(self, event):
        return bool(
            event is not None
            and self._started
            and self.store.accepting
            and self._cfg("enabled", True)
        )

    async def initialize(self):
        self._started = True
        try:
            await self.store.start()
        except Exception as exc:
            # The independent reply filter must remain usable if telemetry fails.
            self.record_failure(exc)
        if not self.probe.install():
            logger.warning("[%s] probe unavailable: %s", PLUGIN_NAME, self.probe.status["reason"])
        elif not self.probe.retry.snapshot()["ok"]:
            logger.warning(
                "[%s] retry coverage incomplete: %s", PLUGIN_NAME, self.probe.retry.status["reason"]
            )
        self.continuations.guard(self.continuations.discover)
        if self.store.accepting:
            self._ensure_reconcile_task()

    async def terminate(self):
        self._started = False
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None
        self.continuations.restore()
        self.probe.restore()
        # AstrBot 4.28 keeps registered handlers in a shared list; remove only ours.
        routes = getattr(self.context, "registered_web_apis", None)
        if isinstance(routes, list):
            routes[:] = [row for row in routes if getattr(row[1], "__self__", None) is not self]
        await self.store.close()

    def record_failure(self, exc):
        self._record_errors += 1
        self._last_record_error = redact_text(
            f"{type(exc).__name__}: {exc}", 2000, self.store.redact_secrets
        )
        if time.monotonic() - self._last_warning >= 30:
            logger.warning("[%s] collection degraded: %s", PLUGIN_NAME, self._last_record_error)
            self._last_warning = time.monotonic()

    def _enqueue(self, kind, payload):
        try:
            return self.store.enqueue(kind, payload)
        except Exception as exc:
            self.record_failure(exc)
            return False

    def _ensure_task(self, event, *, new_cycle=False):
        if not self.capture_enabled(event):
            return None
        state = event.get_extra(TASK_STATE_EXTRA)
        if isinstance(state, dict) and not (new_cycle and state.get("ended")):
            return None if state.get("ended") else state
        identity = event.get_extra(CALLER_EXTRA)
        if not isinstance(identity, dict):
            identity = {}
        state = dict(
            task_id=uuid.uuid4().hex,
            created_at=time.time(),
            llm_seq=0,
            tool_seq=0,
            model_known=False,
            provider_id="",
            provider_model="",
        )
        record = dict(
            task_id=state["task_id"],
            created_at=state["created_at"],
            umo=event.unified_msg_origin,
            platform_name=identity.get("platform_name") or event.get_platform_name(),
            platform_id=identity.get("platform_id") or event.get_platform_id(),
            message_type=identity.get("message_type") or str(event.get_message_type()),
            sender_id=identity.get("sender_id") or event.get_sender_id(),
            sender_name=identity.get("sender_name") or event.get_sender_name(),
            group_id=identity.get("group_id") or event.get_group_id(),
            **self._task_metadata(event),
        )
        if not self._enqueue("task_start", record):
            return None
        event.set_extra(TASK_EXTRA, state["task_id"])
        event.set_extra(TASK_STATE_EXTRA, state)
        self._ensure_reconcile_task()
        return state

    def _task_metadata(self, event):
        metadata = self._continuation_metadata(event)
        if metadata:
            return metadata
        event_type = type(event)
        if event_type.__name__ == "CronMessageEvent" or event_type.__module__.endswith(
            ".cron.events"
        ):
            return {"trigger": "cron"}
        return {}

    def _continuation_metadata(self, event):
        scope = event.get_extra(CONTINUATION_EXTRA)
        if not scope:
            return {}
        return dict(
            parent_task_id=scope["source"].get_extra(TASK_EXTRA),
            trigger="image_result",
            external_task_id=scope["task_id"],
        )

    def _record_task_model(
        self,
        event,
        provider_id,
        provider_model,
        *,
        conversation_id="",
        started_at=None,
        queued_at=None,
    ):
        state = event.get_extra(TASK_STATE_EXTRA)
        provider_model = str(provider_model or "")
        if not isinstance(state, dict) or state.get("ended") or not provider_model:
            return False
        if state.get("model_known"):
            return False
        payload = dict(
            task_id=state["task_id"],
            started_at=started_at or time.time(),
            queued_at=queued_at,
            conversation_id=str(conversation_id or ""),
            provider_id=str(provider_id or ""),
            provider_model=provider_model,
        )
        if not self._enqueue("task_update", payload):
            return False
        state.update(
            model_known=True,
            provider_id=payload["provider_id"],
            provider_model=provider_model,
        )
        return True

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata):
        self.continuations.guard(self.continuations.discover)

    def finish_continuation(self, scope, status):
        if status == "completed":
            status = "completed" if scope["sent"] and not scope["failed"] else "error"
        reason = scope["error"] or (
            "No successful result delivery observed" if status == "error" else ""
        )
        for event in scope["events"]:
            state = event.get_extra(TASK_STATE_EXTRA)
            if not isinstance(state, dict):
                continue
            # Override an early Agent end with the delivery owner's real result.
            if self._enqueue(
                "task_end",
                dict(
                    task_id=state["task_id"], finished_at=time.time(), status=status, error=reason
                ),
            ):
                state["ended"] = True

    def _start_span(self, kind, event, **fields):
        state = self._ensure_task(event)
        if state is None:
            return None
        key = f"{kind}_seq"
        state[key] += 1
        span = dict(
            id=uuid.uuid4().hex,
            task_id=state["task_id"],
            sequence=state[key],
            started_at=time.time(),
            clock=time.perf_counter(),
            **fields,
        )
        return span if self._enqueue(kind + "_start", span) else None

    def start_llm(self, runner, include_model):
        event = event_from(field(runner, "run_context"))
        if not self.capture_enabled(event):
            return None
        provider = field(runner, "provider")
        provider_id = str(field(field(provider, "provider_config", {}), "id", "") or "")
        model = field(field(runner, "req"), "model") if include_model else None
        model = str(model or provider.get_model() or "")
        # AstrBot explicitly sets include_model=False for fallback provider attempts.
        fallback = not include_model
        retry = self.probe.retry.llm_metadata(runner)
        messages = field(field(runner, "run_context"), "messages", [])
        span = self._start_span(
            "llm",
            event,
            provider_id=provider_id,
            provider_model=model,
            attempt_kind="retry" if retry.get("is_retry") else "fallback" if fallback else "round",
            is_fallback=fallback,
            input_text=dict(model=model, messages=list(messages or [])),
            **retry,
        )
        # Agent/Cron runners can bypass on_llm_request. The first actual
        # runner call is still authoritative for the task's displayed model.
        self._record_task_model(
            event,
            provider_id,
            model,
            started_at=span.get("started_at") if span else None,
            queued_at=event.get_extra(QUEUED_EXTRA),
        )
        return span

    def start_attempt(self, call, layer, metadata, *, parent_id=None, operation=""):
        if not call.get("active") or not self.store.accepting or not self._cfg("enabled", True):
            return None
        call["attempt_seq"] = call.get("attempt_seq", 0) + 1
        span = dict(
            id=uuid.uuid4().hex,
            task_id=call["span"]["task_id"],
            llm_call_id=call["span"]["id"],
            parent_id=parent_id,
            layer=layer,
            round_id=call["span"].get("round_id"),
            sequence=call["attempt_seq"],
            operation=operation,
            started_at=time.time(),
            clock=time.perf_counter(),
            **metadata,
        )
        return span if self._enqueue("attempt_start", span) else None

    def finish_attempt(self, span, status, **fields):
        self.finish_span("attempt", span, status, **fields)

    def record_retry_wait(self, span, layer, planned, actual, status):
        if span is not None:
            self._enqueue(
                "retry_wait",
                dict(id=span["id"], layer=layer, planned=planned, actual=actual, status=status),
            )

    def start_tool(self, tool, run_context, tool_args):
        return self._start_span(
            "tool",
            event_from(run_context),
            tool_name=str(field(tool, "name", "unknown")),
            input=tool_args,
        )

    def finish_span(self, kind, span, status, **fields):
        if span is None:
            return
        # Complete already admitted spans even if collection was disabled mid-call.
        self._enqueue(
            kind + "_end",
            dict(
                id=span["id"],
                task_id=span["task_id"],
                status=status,
                finished_at=time.time(),
                duration=max(0, time.perf_counter() - span["clock"]),
                **fields,
            ),
        )

    def end_task(self, event, status, error=""):
        if event is None:
            return
        continuation = event.get_extra(CONTINUATION_EXTRA)
        if continuation and status == "interrupted":
            return  # The delivery owner, not a deliberately closed runner, ends this task.
        state = event.get_extra(TASK_STATE_EXTRA)
        if not isinstance(state, dict) or state.get("ended"):
            return
        if self._enqueue(
            "task_end",
            dict(task_id=state["task_id"], finished_at=time.time(), status=status, error=error),
        ):
            task_id = state["task_id"]
            state.clear()
            state.update(task_id=task_id, ended=True)

    @filter.on_decorating_result(priority=-1000)
    async def on_decorating_result(self, event: AstrMessageEvent):
        if self._cfg("strip_thinking_blocks", True):
            result = event.get_result()
            if result is not None:
                for component in result.chain:
                    if isinstance(component, Plain):
                        component.text = strip_thinking(component.text)

    @filter.on_waiting_llm_request()
    async def on_waiting_llm(self, event: AstrMessageEvent):
        if self.capture_enabled(event):
            event.set_extra(QUEUED_EXTRA, time.time())

    @filter.on_llm_request(priority=10000)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        try:
            state = self._ensure_task(event, new_cycle=True)
            if state is None:
                return
            provider = await self.context.get_using_provider_async(event.unified_msg_origin)
            self._record_task_model(
                event,
                str(field(field(provider, "provider_config", {}), "id", "") or ""),
                str(field(req, "model") or provider.get_model() or ""),
                started_at=time.time(),
                queued_at=event.get_extra(QUEUED_EXTRA),
                conversation_id=str(field(field(req, "conversation"), "cid", "") or ""),
            )
        except Exception as exc:
            self.record_failure(exc)

    @filter.on_agent_begin()
    async def on_agent_begin(self, event: AstrMessageEvent, run_context):
        try:
            self._ensure_task(event)
        except Exception as exc:
            self.record_failure(exc)

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, run_context, response):
        try:
            status = "error" if field(response, "role") == "err" else "completed"
            if field(response, "completion_text") == "Output stopped." or event.get_extra(
                "agent_user_aborted", False
            ):
                status = "aborted"
            self.end_task(
                event,
                status,
                str(field(response, "completion_text", "")) if status == "error" else "",
            )
        except Exception as exc:
            self.record_failure(exc)

    def _active_registry_snapshot(self):
        try:
            from astrbot.core.utils.active_event_registry import active_event_registry

            events, callbacks = (
                active_event_registry._events,
                active_event_registry._agent_stop_callbacks,
            )
            if isinstance(events, dict) and isinstance(callbacks, dict):
                return events, callbacks
        except (ImportError, AttributeError):
            pass
        return None

    def _task_is_live(self, task, snapshot):
        if any(
            scope["active"]
            and any(event.get_extra(TASK_EXTRA) == task["task_id"] for event in scope["events"])
            for scope in self.continuations.active
        ):
            return True
        events, callbacks = snapshot
        candidates = list(events.get(task.get("umo") or "", ())) + list(callbacks)
        for event in candidates:
            try:
                if event.get_extra(TASK_EXTRA) == task["task_id"]:
                    return True
            except Exception:
                continue
        return False

    async def _reconcile_running_tasks(self, tasks):
        snapshot = self._active_registry_snapshot()
        if snapshot is None:
            return
        now = time.time()
        for task in tasks:
            if now - (task.get("started_at") or task["created_at"]) >= RECONCILE_GRACE_SECONDS:
                if not self._task_is_live(task, snapshot):
                    self.store.enqueue_recovered(
                        task["task_id"], "No active AstrBot event or runner matches this task ID"
                    )

    def _ensure_reconcile_task(self):
        if self._started and (self._reconcile_task is None or self._reconcile_task.done()):
            self._reconcile_task = asyncio.create_task(
                self._reconcile_loop(), name="llm-monitor-recovery"
            )

    async def _reconcile_loop(self):
        await asyncio.sleep(RECONCILE_STARTUP_DELAY_SECONDS)
        while self._started:
            try:
                tasks = await self.store.list_running_tasks()
                await self._reconcile_running_tasks(tasks)
                self._recovery_error = ""
            except Exception as exc:
                self._recovery_error = redact_text(str(exc), 2000, self.store.redact_secrets)
                self.record_failure(exc)
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)

    def _health(self):
        enabled = bool(self._cfg("enabled", True))
        storage = self.store.health()
        probe = self.probe.snapshot()
        retry = self.probe.retry.snapshot()
        registry = self._active_registry_snapshot() is not None
        ok = self._started and storage["ok"] and not self._record_errors
        ok = ok and (
            not enabled
            or (probe["enabled"] and retry["ok"] and registry and not self._recovery_error)
        )
        status = (
            "stopped"
            if not self._started
            else "degraded"
            if not ok
            else "healthy"
            if enabled
            else "disabled"
        )
        return dict(
            ok=bool(ok),
            status=status,
            plugin=PLUGIN_NAME,
            version=VERSION,
            astrbot_version=__version__,
            started=self._started,
            enabled=enabled,
            collecting=bool(
                enabled and self._started and storage["accepting"] and probe["enabled"]
            ),
            reply_filter=bool(self._cfg("strip_thinking_blocks", True)),
            storage=storage,
            probe=probe,
            retry_coverage=retry,
            continuation_coverage=self.continuations.snapshot(),
            recovery=dict(ok=registry and not self._recovery_error, error=self._recovery_error),
            record_errors=self._record_errors,
            last_record_error=self._last_record_error,
        )

    async def health(self):
        return json_response(self._health())

    async def self_check_api(self):
        health = self._health()
        return json_response(dict(checked_at=time.time(), **health))

    @staticmethod
    def _number(name, default, minimum, maximum):
        try:
            value = int(request.query.get(name, default))
        except (ValueError, TypeError):
            value = default
        return max(minimum, min(value, maximum))

    def _query(self):
        status = request.query.get("status", "")

        def multi(name, limit):
            raw = request.query.get(name, "")
            try:
                value = json.loads(raw) if raw.startswith("[") else raw
            except (TypeError, ValueError):
                value = raw
            if isinstance(value, list):
                return [str(item)[:limit] for item in value if str(item).strip()][:20]
            return str(value)[:limit] if value else ""

        return dict(
            hours=self._number("hours", 24, 0, 8760),
            status=status if status in VALID_STATUSES else "",
            model=multi("model", 200),
            platform=multi("platform", 100),
        )

    async def _respond(self, operation):
        try:
            result = await operation
            return (
                json_response(result)
                if result is not None
                else json_response({"error": "Task not found"}, status_code=404)
            )
        except Exception as exc:
            self.record_failure(exc)
            return json_response(
                {"error": "Monitor storage unavailable; see health diagnostics"}, status_code=503
            )

    async def tasks_api(self):
        return await self._respond(
            self.store.list_tasks(
                limit=self._number("limit", 50, 1, 200),
                offset=self._number("offset", 0, 0, 10000000),
                **self._query(),
            )
        )

    async def summary_api(self):
        return await self._respond(self.store.summary(**self._query()))

    async def filters_api(self):
        return await self._respond(
            self.store.filter_options(hours=self._number("hours", 24, 0, 8760))
        )

    async def task_api(self, task_id: str):
        return await self._respond(self.store.get_task(task_id))

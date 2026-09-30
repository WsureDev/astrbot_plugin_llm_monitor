from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot import __version__
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import json_response, request

from .store import EventStore


PLUGIN_NAME = "astrbot_plugin_llm_monitor"
TASK_EXTRA = f"{PLUGIN_NAME}.task_id"
TASK_STATE_EXTRA = f"{PLUGIN_NAME}.task_state"


def _now() -> float:
    return time.time()


def _json_safe(value: Any, max_chars: int, redact: bool) -> str:
    """Serialize observability payloads without allowing them to break the agent."""
    try:
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        elif hasattr(value, "__dict__") and not isinstance(value, (str, bytes)):
            value = value.__dict__
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if redact:
        secret_keys = "api_key|apikey|authorization|access_token|secret|password|token"
        text = re.sub(
            rf'("(?:{secret_keys})"\s*:\s*)"(?:[^"\\]|\\.)*"',
            r'\1"[REDACTED]"',
            text,
            flags=re.IGNORECASE,
        )
    limit = max(1000, int(max_chars or 20000))
    if len(text) > limit:
        return text[:limit] + "…[truncated]"
    return text


class RunnerProbe:
    """Best-effort wrapper for the internal Agent LLM iterator."""

    def __init__(self, plugin: "LLMMonitorPlugin") -> None:
        self.plugin = plugin
        self.target = None
        self.original = None
        self.wrapper = None
        self.original_step = None
        self.step_wrapper = None
        self.status: dict[str, Any] = {
            "name": "tool_loop_agent_runner._iter_llm_responses",
            "enabled": False,
            "reason": "not checked",
        }

    def install(self) -> bool:
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import (
                ToolLoopAgentRunner,
            )

            original = getattr(ToolLoopAgentRunner, "_iter_llm_responses", None)
            if not callable(original):
                return self._disable("target method is missing")
            signature = inspect.signature(original)
            if "include_model" not in signature.parameters:
                return self._disable("target signature has no include_model parameter")
            if not inspect.isasyncgenfunction(original):
                return self._disable("target is not an async generator")
            if getattr(original, "_astrbot_llm_monitor_wrapper", False):
                self.status.update(enabled=True, reason="already wrapped")
                return True
            original_step = getattr(ToolLoopAgentRunner, "step", None)
            if not callable(original_step) or not inspect.isasyncgenfunction(original_step):
                return self._disable("step target is missing or not an async generator")

            plugin = self.plugin

            async def wrapped(runner, *, include_model: bool = True):
                started = _now()
                response_usage: dict[str, Any] = {}
                first_token_at = 0.0
                status = "completed"
                error_text = ""
                provider = getattr(runner, "provider", None)
                provider_config = getattr(provider, "provider_config", {}) or {}
                provider_id = str(provider_config.get("id", "") or "")
                provider_model = ""
                try:
                    provider_model = str(provider.get_model() or "")
                except Exception:
                    provider_model = ""
                req = getattr(runner, "req", None)
                effective_model = str(getattr(req, "model", None) or provider_model)
                event = None
                run_context = getattr(runner, "run_context", None)
                context = getattr(run_context, "context", None)
                event = getattr(context, "event", None)
                task_id = plugin._task_id(event)
                call_id = uuid.uuid4().hex
                sequence = plugin._next_llm_sequence(task_id)
                plugin._safe_enqueue(
                    "llm_start",
                    {
                        "id": call_id,
                        "task_id": task_id,
                        "sequence": sequence,
                        "started_at": started,
                        "provider_id": provider_id,
                        "provider_model": effective_model,
                    },
                )
                try:
                    async for response in original(runner, include_model=include_model):
                        if getattr(response, "is_chunk", False) and not first_token_at:
                            first_token_at = _now()
                        usage = getattr(response, "usage", None)
                        if usage is not None and not getattr(response, "is_chunk", False):
                            response_usage = dict(getattr(usage, "__dict__", {}) or {})
                        if getattr(response, "role", "") == "err":
                            status = "error"
                        yield response
                except asyncio.CancelledError:
                    status = "cancelled"
                    raise
                except Exception as exc:
                    status = "error"
                    error_text = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    finished = _now()
                    plugin._safe_enqueue(
                        "llm_end",
                        {
                            "id": call_id,
                            "task_id": task_id,
                            "finished_at": finished,
                            "status": status,
                            "duration": max(0.0, finished - started),
                            "ttft": max(0.0, first_token_at - started) if first_token_at else 0.0,
                            "provider_id": provider_id,
                            "provider_model": effective_model,
                            "usage": response_usage,
                            "error": error_text,
                        },
                    )

            wrapped._astrbot_llm_monitor_wrapper = True
            wrapped._astrbot_llm_monitor_original = original

            async def wrapped_step(runner):
                run_context = getattr(runner, "run_context", None)
                context = getattr(run_context, "context", None)
                event = getattr(context, "event", None)
                try:
                    async for response in original_step(runner):
                        response_type = getattr(response, "type", "")
                        if response_type == "err":
                            plugin._end_task(event, "error")
                        elif response_type == "aborted":
                            plugin._end_task(event, "aborted")
                        yield response
                except asyncio.CancelledError:
                    plugin._end_task(event, "cancelled")
                    raise

            wrapped_step._astrbot_llm_monitor_wrapper = True
            wrapped_step._astrbot_llm_monitor_original = original_step
            ToolLoopAgentRunner._iter_llm_responses = wrapped
            ToolLoopAgentRunner.step = wrapped_step
            self.target = ToolLoopAgentRunner
            self.original = original
            self.wrapper = wrapped
            self.original_step = original_step
            self.step_wrapper = wrapped_step
            self.status.update(enabled=True, reason="installed", version=__version__)
            return True
        except Exception as exc:
            return self._disable(f"install failed: {type(exc).__name__}: {exc}")

    def _disable(self, reason: str) -> bool:
        self.status.update(enabled=False, reason=reason)
        logger.warning("[%s] LLM probe disabled: %s", PLUGIN_NAME, reason)
        return False

    def restore(self) -> None:
        if self.target is None or self.original is None or self.wrapper is None:
            return
        try:
            if getattr(self.target, "_iter_llm_responses", None) is self.wrapper:
                self.target._iter_llm_responses = self.original
            if self.original_step is not None and getattr(self.target, "step", None) is self.step_wrapper:
                self.target.step = self.original_step
        except Exception:
            logger.exception("[%s] failed to restore LLM probe", PLUGIN_NAME)
        finally:
            self.target = None
            self.original = None
            self.wrapper = None
            self.original_step = None
            self.step_wrapper = None


class LLMMonitorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.store = EventStore(
            self.data_dir / "events.sqlite3",
            max_text_chars=int(self._cfg("max_text_chars", 20000) or 20000),
            retention_days=int(self._cfg("retention_days", 30) or 30),
            redact_secrets=bool(self._cfg("redact_secrets", True)),
        )
        self.probe = RunnerProbe(self)
        self.self_check: dict[str, Any] = {}
        self._llm_sequences: dict[str, int] = {}
        self._tool_sequences: dict[str, int] = {}
        self._started = False

        prefix = f"/{PLUGIN_NAME}"
        context.register_web_api(f"{prefix}/health", self.health, ["GET"], "LLM monitor health")
        context.register_web_api(f"{prefix}/self-check", self.self_check_api, ["GET"], "LLM monitor self-check")
        context.register_web_api(f"{prefix}/summary", self.summary_api, ["GET"], "LLM monitor summary")
        context.register_web_api(f"{prefix}/tasks", self.tasks_api, ["GET"], "LLM monitor tasks")
        context.register_web_api(f"{prefix}/tasks/<task_id>", self.task_api, ["GET"], "LLM monitor task")

    def _cfg(self, key: str, default: Any) -> Any:
        section = self.config.get("monitor", {})
        if isinstance(section, dict):
            return section.get(key, default)
        return default

    async def initialize(self) -> None:
        self._started = True
        await self.store.start()
        enabled = bool(self._cfg("enabled", True))
        if enabled:
            self.probe.install()
        else:
            self.probe.status.update(enabled=False, reason="disabled by config")
        self.self_check = self._build_self_check()
        logger.info("[%s] loaded; probe=%s data=%s", PLUGIN_NAME, self.probe.status, self.data_dir)

    async def terminate(self) -> None:
        self._started = False
        self.probe.restore()
        await self.store.close()

    def _build_self_check(self) -> dict[str, Any]:
        checks = {
            "astrbot_version": {"ok": True, "value": __version__},
            "llm_probe": dict(self.probe.status),
            "tool_hooks": {"ok": True, "value": "registered by AstrBot event decorators"},
            "storage": {"ok": self.store.path.parent.exists(), "value": str(self.store.path)},
        }
        return {
            "plugin": PLUGIN_NAME,
            "checked_at": _now(),
            "checks": checks,
            "repair_items": [
                f"{name}: {item.get('reason', 'check failed')}"
                for name, item in checks.items()
                if item.get("ok") is False or item.get("enabled") is False
            ],
        }

    def _safe_enqueue(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self.store.enqueue(kind, payload)
        except Exception:
            logger.exception("[%s] monitor record enqueue failed", PLUGIN_NAME)

    def _end_task(self, event: AstrMessageEvent | None, status: str) -> None:
        try:
            task_id = self._task_id(event)
            state = self._task_state(event)
            if state.get("ended"):
                return
            state["ended"] = True
            self._safe_enqueue("task_end", {"task_id": task_id, "finished_at": _now(), "status": status})
            self.logger.info("LLM task end task=%s status=%s", task_id, status)
        except Exception:
            logger.exception("[%s] task end recording failed", PLUGIN_NAME)

    def _task_id(self, event: AstrMessageEvent | None) -> str:
        if event is None:
            return f"unknown-{uuid.uuid4().hex}"
        task_id = event.get_extra(TASK_EXTRA)
        if task_id:
            return str(task_id)
        task_id = uuid.uuid4().hex
        event.set_extra(TASK_EXTRA, task_id)
        state = {
            "task_id": task_id,
            "created_at": _now(),
            "llm_seq": 0,
            "tool_seq": 0,
            "pending_tools": [],
        }
        event.set_extra(TASK_STATE_EXTRA, state)
        self._safe_enqueue("task_start", self._task_from_event(task_id, event, state))
        return task_id

    def _task_state(self, event: AstrMessageEvent | None) -> dict[str, Any]:
        if event is None:
            return {"task_id": self._task_id(None), "llm_seq": 0, "tool_seq": 0, "pending_tools": []}
        state = event.get_extra(TASK_STATE_EXTRA)
        if not isinstance(state, dict):
            self._task_id(event)
            state = event.get_extra(TASK_STATE_EXTRA)
        return state if isinstance(state, dict) else {"llm_seq": 0, "tool_seq": 0, "pending_tools": []}

    def _next_llm_sequence(self, task_id: str) -> int:
        value = self._llm_sequences.get(task_id, 0) + 1
        self._llm_sequences[task_id] = value
        return value

    def _next_tool_sequence(self, task_id: str) -> int:
        value = self._tool_sequences.get(task_id, 0) + 1
        self._tool_sequences[task_id] = value
        return value

    def _task_from_event(self, task_id: str, event: AstrMessageEvent, state: dict[str, Any]) -> dict[str, Any]:
        conversation_id = ""
        return {
            "task_id": task_id,
            "created_at": state.get("created_at", _now()),
            "umo": event.unified_msg_origin,
            "platform_name": event.get_platform_name(),
            "platform_id": event.get_platform_id(),
            "message_type": str(event.get_message_type()),
            "sender_id": event.get_sender_id(),
            "sender_name": event.get_sender_name(),
            "group_id": event.get_group_id(),
            "conversation_id": conversation_id,
            "status": "running",
        }

    @filter.on_waiting_llm_request()
    async def on_waiting_llm(self, event: AstrMessageEvent) -> None:
        try:
            event.set_extra(f"{PLUGIN_NAME}.queued_at", _now())
        except Exception:
            logger.exception("[%s] waiting hook failed", PLUGIN_NAME)

    @filter.on_llm_request(priority=10_000)
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        try:
            task_id = self._task_id(event)
            state = self._task_state(event)
            state["conversation_id"] = str(getattr(getattr(req, "conversation", None), "cid", "") or "")
            provider = await self.context.get_using_provider_async(event.unified_msg_origin)
            provider_config = getattr(provider, "provider_config", {}) or {}
            state["provider_id"] = str(provider_config.get("id", "") or "")
            state["provider_model"] = str(getattr(provider, "get_model", lambda: "")() or "")
            self._safe_enqueue(
                "task_update",
                {
                    "task_id": task_id,
                    "conversation_id": state["conversation_id"],
                    "provider_id": state["provider_id"],
                    "provider_model": state["provider_model"],
                    "started_at": _now(),
                    "queued_at": event.get_extra(f"{PLUGIN_NAME}.queued_at", 0.0),
                },
            )
            self.logger.info("LLM task start task=%s provider=%s model=%s umo=%s", task_id, state["provider_id"], state["provider_model"], event.unified_msg_origin)
        except Exception:
            logger.exception("[%s] LLM request hook failed", PLUGIN_NAME)

    @filter.on_agent_begin()
    async def on_agent_begin(self, event: AstrMessageEvent, run_context) -> None:
        try:
            self._task_id(event)
        except Exception:
            logger.exception("[%s] agent begin hook failed", PLUGIN_NAME)

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, run_context, response) -> None:
        try:
            status = "error" if getattr(response, "role", "") == "err" else "completed"
            if getattr(response, "completion_text", "") == "Output stopped.":
                status = "aborted"
            if event.get_extra("agent_user_aborted", False):
                status = "aborted"
            self._end_task(event, status)
        except Exception:
            logger.exception("[%s] agent done hook failed", PLUGIN_NAME)

    @filter.on_using_llm_tool()
    async def on_tool_start(self, event: AstrMessageEvent, tool, tool_args) -> None:
        try:
            task_id = self._task_id(event)
            call_id = uuid.uuid4().hex
            sequence = self._next_tool_sequence(task_id)
            state = self._task_state(event)
            state.setdefault("pending_tools", []).append(call_id)
            self._safe_enqueue(
                "tool_start",
                {
                    "id": call_id,
                    "task_id": task_id,
                    "sequence": sequence,
                    "started_at": _now(),
                    "tool_name": str(getattr(tool, "name", "unknown")),
                    "input_json": _json_safe(tool_args, int(self._cfg("max_text_chars", 20000)), bool(self._cfg("redact_secrets", True))),
                },
            )
        except Exception:
            logger.exception("[%s] tool start hook failed", PLUGIN_NAME)

    @filter.on_llm_tool_respond()
    async def on_tool_end(self, event: AstrMessageEvent, tool, tool_args, tool_result) -> None:
        try:
            task_id = self._task_id(event)
            state = self._task_state(event)
            pending = state.setdefault("pending_tools", [])
            call_id = pending.pop(0) if pending else uuid.uuid4().hex
            self._safe_enqueue(
                "tool_end",
                {
                    "id": call_id,
                    "task_id": task_id,
                    "finished_at": _now(),
                    "tool_name": str(getattr(tool, "name", "unknown")),
                    "output_json": _json_safe(tool_result, int(self._cfg("max_text_chars", 20000)), bool(self._cfg("redact_secrets", True))),
                    "status": "completed" if tool_result is not None else "empty",
                },
            )
        except Exception:
            logger.exception("[%s] tool end hook failed", PLUGIN_NAME)

    async def health(self):
        return json_response({"ok": True, "plugin": PLUGIN_NAME, "version": "0.1.0", "started": self._started})

    async def self_check_api(self):
        self.self_check = self._build_self_check()
        return json_response(self.self_check)

    async def tasks_api(self):
        limit = max(1, min(request.query.get("limit", 50, type=int), 200))
        offset = max(0, request.query.get("offset", 0, type=int))
        status = request.query.get("status", "")
        model = request.query.get("model", "")
        platform = request.query.get("platform", "")
        return json_response(await self.store.list_tasks(limit, offset, status, model, platform))

    async def summary_api(self):
        hours = max(1, min(request.query.get("hours", 24, type=int), 24 * 365))
        return json_response(await self.store.summary(hours))

    async def task_api(self, task_id: str):
        result = await self.store.get_task(task_id)
        if result is None:
            return json_response({"error": "task not found"}, status_code=404)
        return json_response(result)

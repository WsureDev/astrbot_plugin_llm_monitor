"""Fail-open instrumentation for AstrBot 4.28's runner and tool executor.

Each tool execution owns its ID. Public start/end hooks cannot reliably pair
calls because AstrBot skips the end hook after an exception.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import time

from .input_probe import ProviderInputProbe
from .payloads import ResponseCapture
from .retry_probe import CALL, RetryProbe, scoped_iterator
from .run_scope import RUN_SCOPE
from .wrapping import active_monitor_wrapper, contains_wrapper


def field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def event_from(context):
    return field(field(context, "context"), "event")


async def passthrough(iterator):
    try:
        async for item in iterator:
            yield item
    finally:
        await iterator.aclose()


class RunnerProbe:
    def __init__(self, plugin):
        self.plugin = plugin
        self.retry = RetryProbe(plugin)
        self.inputs = ProviderInputProbe(self)
        self._patches = []
        self.active = False
        self._generation = None
        self.status = dict(enabled=False, reason="not initialized", tested_version="4.28.0")

    def _record(self, method, *args, **kwargs):
        if not self.active:
            return None
        try:
            return getattr(self.plugin, method)(*args, **kwargs)
        except Exception as exc:
            self.plugin.record_failure(exc)
            return None

    def _record_scoped(self, scoped, method, *args, **kwargs):
        token = RUN_SCOPE.set(scoped) if scoped is not None else None
        try:
            return self._record(method, *args, **kwargs)
        finally:
            if token is not None:
                RUN_SCOPE.reset(token)

    def install(self, runner_class=None, executor_class=None):
        if self._patches:
            return True
        try:
            runtime_install = runner_class is None
            if runner_class is None:
                from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner

                runner_class = ToolLoopAgentRunner
            if executor_class is None:
                from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

                executor_class = FunctionToolExecutor
            llm = runner_class._iter_llm_responses
            step = runner_class.step
            descriptor = inspect.getattr_static(executor_class, "execute")
            if not isinstance(descriptor, classmethod):
                raise TypeError("FunctionToolExecutor.execute is no longer a classmethod")
            execute = descriptor.__func__
            for method, required in (
                (llm, {"include_model"}),
                (step, set()),
                (execute, {"tool", "run_context", "tool_args"}),
            ):
                if not inspect.isasyncgenfunction(method):
                    raise TypeError("probe target is not an async generator")
                if not required.issubset(inspect.signature(method).parameters):
                    raise TypeError("probe target signature changed")
                if active_monitor_wrapper(method):
                    raise RuntimeError(
                        "another monitor instance owns a probe; reload that instance first"
                    )
            generation = object()
            self._generation = generation

            @functools.wraps(llm)
            async def wrapped_llm(runner, *, include_model=True):
                if not self.active or self._generation is not generation:
                    async for item in passthrough(llm(runner, include_model=include_model)):
                        yield item
                    return
                span = self._record("start_llm", runner, include_model)
                call = self.retry.guard(lambda: self.retry.begin_call(runner, span))
                if call is not None:
                    self.retry.guard(lambda: self.inputs.ensure_provider(type(call["provider"])))
                iterator = scoped_iterator(llm(runner, include_model=include_model), CALL, call)
                status, error, ttft, usage, response_model = "interrupted", "", None, {}, ""
                capture = ResponseCapture(
                    redact=getattr(getattr(self.plugin, "store", None), "redact_secrets", True)
                )
                try:
                    async for response in iterator:
                        if span:
                            try:
                                chunk = field(response, "is_chunk", False)
                                capture.feed(response)
                                content = (
                                    field(response, "completion_text")
                                    or field(response, "reasoning_content")
                                    or field(response, "tools_call_name")
                                )
                                if chunk and content and ttft is None:
                                    ttft = time.perf_counter() - span["clock"]
                                if not chunk:
                                    raw_usage = field(response, "usage")
                                    if raw_usage is not None:
                                        usage = {
                                            key: field(raw_usage, key, 0)
                                            for key in ("input_other", "input_cached", "output")
                                        }
                                    raw = field(response, "raw_completion")
                                    response_model = (
                                        field(raw, "model")
                                        or field(raw, "model_version")
                                        or response_model
                                    )
                                    if status != "error":
                                        status = "completed"
                                if field(response, "role") == "err":
                                    status, error = (
                                        "error",
                                        str(field(response, "completion_text", "Provider error")),
                                    )
                            except Exception as exc:
                                self.plugin.record_failure(exc)
                        yield response
                    if status == "interrupted":
                        status = "completed" if ttft is not None else "empty"
                        try:
                            if getattr(runner, "_is_stop_requested", lambda: False)():
                                status = "aborted"
                        except Exception as exc:
                            self.plugin.record_failure(exc)
                except asyncio.CancelledError:
                    status = "cancelled"
                    raise
                except Exception as exc:
                    status, error = "error", f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    try:
                        await iterator.aclose()
                    finally:
                        if call is not None:
                            call["active"] = False
                        self._record(
                            "finish_span",
                            "llm",
                            span,
                            status,
                            error=error,
                            ttft=ttft,
                            usage=usage,
                            response_model=response_model,
                            output_text=capture.result(),
                        )

            @functools.wraps(execute)
            async def wrapped_execute(cls, tool, run_context, **tool_args):
                if not self.active or self._generation is not generation:
                    async for item in passthrough(execute(cls, tool, run_context, **tool_args)):
                        yield item
                    return
                span = self._record("start_tool", tool, run_context, tool_args)
                iterator = execute(cls, tool, run_context, **tool_args)
                status, error, last_output, saw_output, is_error = (
                    "interrupted",
                    "",
                    None,
                    False,
                    False,
                )
                try:
                    async for result in iterator:
                        if span:
                            last_output = result
                            saw_output |= result is not None
                            is_error |= bool(field(result, "isError", False))
                        yield result
                    status = "error" if is_error else "completed" if saw_output else "empty"
                    if is_error:
                        error = "Tool returned isError=true; see output"
                except asyncio.CancelledError:
                    status = "cancelled"
                    raise
                except Exception as exc:
                    status, error = "error", f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    try:
                        await iterator.aclose()
                    finally:
                        self._record(
                            "finish_span", "tool", span, status, error=error, output=last_output
                        )

            @functools.wraps(step)
            async def wrapped_step(runner):
                if not self.active or self._generation is not generation:
                    async for item in passthrough(step(runner)):
                        yield item
                    return
                event = event_from(field(runner, "run_context"))
                binder = getattr(self.plugin, "bind_runner", None)
                scoped = self._record("bind_runner", runner) if callable(binder) else None
                iterator = step(runner)
                if scoped is not None:
                    iterator = scoped_iterator(iterator, RUN_SCOPE, scoped)
                exhausted = False
                try:
                    async for response in iterator:
                        kind = field(response, "type")
                        if kind in {"err", "aborted"}:
                            self._record_scoped(
                                scoped, "end_task", event, "error" if kind == "err" else "aborted"
                            )
                        yield response
                    exhausted = True
                except asyncio.CancelledError:
                    self._record_scoped(scoped, "end_task", event, "cancelled")
                    raise
                except Exception as exc:
                    self._record_scoped(
                        scoped, "end_task", event, "error", f"{type(exc).__name__}: {exc}"
                    )
                    raise
                finally:
                    try:
                        await iterator.aclose()
                    finally:
                        done = bool(getattr(runner, "done", lambda: False)())
                        if done or not exhausted:
                            if done:
                                final = getattr(runner, "get_final_llm_resp", lambda: None)()
                                final_status = (
                                    "error" if field(final, "role") == "err" else "completed"
                                )
                                self._record_scoped(scoped, "end_task", event, final_status)
                            else:
                                self._record_scoped(scoped, "end_task", event, "interrupted")
                        releaser = getattr(self.plugin, "release_runner", None)
                        if scoped is not None and (done or not exhausted) and callable(releaser):
                            self._record("release_runner", scoped)

            for wrapper in (wrapped_llm, wrapped_step, wrapped_execute):
                wrapper._astrbot_llm_monitor_wrapper = True
                wrapper._astrbot_llm_monitor_owner = self
                wrapper._astrbot_monitor_generation = generation
            replacements = [
                (runner_class, "_iter_llm_responses", llm, wrapped_llm),
                (runner_class, "step", step, wrapped_step),
                (executor_class, "execute", descriptor, classmethod(wrapped_execute)),
            ]
            self.active = True
            for target, name, original, wrapper in replacements:
                setattr(target, name, wrapper)
                self._patches.append((target, name, original, wrapper))
            if runtime_install:
                self.retry.install(runner_class)
            self.status.update(enabled=True, reason="installed")
            return True
        except Exception as exc:
            self.restore()
            self.status.update(enabled=False, reason=f"{type(exc).__name__}: {exc}")
            return False

    def restore(self):
        self.active = False
        self.inputs.restore()
        self.retry.restore()
        for target, name, original, wrapper in reversed(self._patches):
            if inspect.getattr_static(target, name) is wrapper:
                setattr(target, name, original)
        self._patches.clear()
        self.status.update(enabled=False, reason="uninstalled")

    def snapshot(self):
        status = dict(self.status)
        status["provider_input_intact"] = self.inputs.intact()
        if self._patches and any(
            not contains_wrapper(inspect.getattr_static(target, name, None), wrapper)
            for target, name, _, wrapper in self._patches
        ):
            status.update(enabled=False, reason="A probe target was replaced after installation")
        return status

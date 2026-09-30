"""Observe AstrBot retry boundaries without implementing or changing retry policy.

Context is bound only while an async generator advances, never while it yields
control to its consumer. Nested tasks inherit the current call, and all records
are ignored after that call closes. No request URL, headers or body is stored.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import time
import uuid
from contextvars import ContextVar

ROUND = ContextVar("llm_monitor_round", default=None)
CALL = ContextVar("llm_monitor_call", default=None)
PROVIDER = ContextVar("llm_monitor_provider", default=None)
REQUEST = ContextVar("llm_monitor_request", default=None)
PROVIDER_MODULES = {
    "astrbot.core.provider.sources.openai_source",
    "astrbot.core.provider.sources.openai_responses_source",
    "astrbot.core.provider.sources.anthropic_source",
    "astrbot.core.provider.sources.gemini_source",
    "astrbot.core.provider.sources.ssycloud_source",
}


def error_text(error):
    return f"{type(error).__name__}: {error}" if error is not None else ""


async def scoped_iterator(iterator, variable, value):
    try:
        while True:
            token = variable.set(value)
            try:
                result = await anext(iterator)
            except StopAsyncIteration:
                return
            finally:
                variable.reset(token)
            yield result
    finally:
        token = variable.set(value)
        try:
            await iterator.aclose()
        finally:
            variable.reset(token)


class ObservedAttempt:
    def __init__(self, manager, observer):
        self.manager, self.observer = manager, observer
        self.span = None
        self.token = None

    def __getattr__(self, name):
        return getattr(self.manager, name)

    def __enter__(self):
        result = self.manager.__enter__()
        self.observer.probe.guard(self.start)
        return result

    def start(self):
        group = self.observer
        number = self.manager.retry_state.attempt_number
        metadata = dict(
            retry_group_id=group.id,
            attempt_number=number,
            is_retry=number > 1,
            retry_reason=group.reason if number > 1 else "",
            retry_wait=group.wait_actual if number > 1 else 0,
            retry_wait_planned=group.wait_planned if number > 1 else None,
            wait_kind="backoff",
        )
        if group.layer == "framework":
            group.scope["attempt"] = metadata
            group.scope["group"] = group
        else:
            parent = PROVIDER.get()
            self.span = group.probe.record(
                "start_attempt",
                group.scope,
                "request",
                metadata,
                parent_id=parent["span"]["id"] if parent and parent.get("span") else None,
                operation=group.label,
            )
            group.last_span = self.span
            self.token = REQUEST.set(
                dict(call=group.scope, span=self.span, http={}, owner=group.probe)
            )

    def __exit__(self, kind, error, traceback):
        try:
            # Tenacity owns suppression, terminal errors and its outcome state.
            return self.manager.__exit__(kind, error, traceback)
        finally:
            group = self.observer
            group.reason = error_text(error)
            if group.layer == "request":
                status = (
                    "cancelled"
                    if isinstance(error, asyncio.CancelledError)
                    else "error"
                    if error
                    else "completed"
                )
                group.probe.record("finish_attempt", self.span, status, error=group.reason)
                if self.token is not None:
                    REQUEST.reset(self.token)


class ObservedRetrying:
    """Delegate iteration and context suppression to the original Tenacity instance."""

    def __init__(self, original, probe, scope, layer, label=""):
        self.original, self.probe, self.scope = original, probe, scope
        self.layer, self.label = layer, label
        self.id = uuid.uuid4().hex
        self.reason = ""
        self.wait_actual = 0
        self.wait_planned = None
        self.last_span = None
        before_sleep, sleep = original.before_sleep, original.sleep

        async def observe_before_sleep(state):
            if before_sleep is not None:
                result = before_sleep(state)
                if inspect.isawaitable(result):
                    await result

            def note():
                self.wait_planned = float(state.next_action.sleep)
                self.reason = error_text(state.outcome.exception())
                self.probe.record(
                    "record_retry_wait",
                    self.last_span,
                    self.layer,
                    self.wait_planned,
                    None,
                    "waiting",
                )

            self.probe.guard(note)

        async def observe_sleep(delay):
            started, status = time.perf_counter(), "completed"
            try:
                result = sleep(delay)
                return await result if inspect.isawaitable(result) else result
            except BaseException:
                status = "cancelled"
                raise
            finally:
                self.wait_actual = max(0, time.perf_counter() - started)
                self.probe.record(
                    "record_retry_wait",
                    self.last_span,
                    self.layer,
                    self.wait_planned,
                    self.wait_actual,
                    status,
                )

        original.before_sleep, original.sleep = observe_before_sleep, observe_sleep

    def __aiter__(self):
        self.iterator = self.original.__aiter__()
        return self

    async def __anext__(self):
        return ObservedAttempt(await self.iterator.__anext__(), self)

    def __getattr__(self, name):
        return getattr(self.original, name)


class RetryProbe:
    def __init__(self, plugin):
        self.plugin = plugin
        self._patches = []
        self._provider_classes = set()
        self.status = dict(framework=False, request=False, http=False, reason="not initialized")

    def guard(self, operation):
        try:
            return operation()
        except Exception as exc:
            self.plugin.record_failure(exc)
            return None

    def record(self, method, *args, **kwargs):
        return self.guard(lambda: getattr(self.plugin, method)(*args, **kwargs))

    def _patch(self, target, name, wrapper):
        original = inspect.getattr_static(target, name)
        if getattr(original, "_astrbot_retry_monitor_wrapper", False):
            raise RuntimeError(f"Another retry monitor owns {name}")
        local = name in vars(target)
        wrapper._astrbot_retry_monitor_wrapper = True
        setattr(target, name, wrapper)
        self._patches.append((target, name, original, wrapper, local))

    def install(self, runner_class, runner_module=None, request_module=None, http_class=None):
        if self._patches:
            return True
        try:
            if runner_module is None:
                from astrbot.core.agent.runners import tool_loop_agent_runner as runner_module
            if request_module is None:
                from astrbot.core.provider.sources import request_retry as request_module
            if http_class is None:
                from httpx import AsyncClient

                http_class = AsyncClient
            round_method = runner_class._iter_llm_responses_with_fallback
            factory = runner_module.AsyncRetrying
            build = request_module._build_retrying
            send = http_class.send
            if not inspect.isasyncgenfunction(round_method) or not inspect.iscoroutinefunction(
                send
            ):
                raise TypeError("Retry target iterator / HTTP signature changed")
            if not {"provider_label", "max_attempts", "retry_rate_limits"}.issubset(
                inspect.signature(build).parameters
            ):
                raise TypeError("AstrBot request retry factory signature changed")

            @functools.wraps(round_method)
            async def wrapped_round(runner):
                scope = dict(id=uuid.uuid4().hex, runner=runner, owner=self, active=True)
                iterator = scoped_iterator(round_method(runner), ROUND, scope)
                try:
                    async for response in iterator:
                        yield response
                finally:
                    scope["active"] = False
                    await iterator.aclose()

            @functools.wraps(factory)
            def wrapped_factory(*args, **kwargs):
                original = factory(*args, **kwargs)
                scope = ROUND.get()
                if scope and scope.get("owner") is self and scope.get("active"):
                    observed = self.guard(
                        lambda: ObservedRetrying(original, self, scope, "framework")
                    )
                    return observed if observed is not None else original
                return original

            @functools.wraps(build)
            def wrapped_build(provider_label, *, retry_rate_limits, max_attempts=None):
                original = build(
                    provider_label, retry_rate_limits=retry_rate_limits, max_attempts=max_attempts
                )
                call = self.current_call()
                if call:
                    observed = self.guard(
                        lambda: ObservedRetrying(
                            original, self, call, "request", str(provider_label)
                        )
                    )
                    return observed if observed is not None else original
                return original

            @functools.wraps(send)
            async def wrapped_send(client, request, *args, **kwargs):
                scope = REQUEST.get()
                call = self.current_call()
                span, bucket = None, None
                if call and scope and scope.get("owner") is self and scope.get("call") is call:
                    prepared = self.guard(lambda: self._start_http(scope, request))
                    if prepared is not None:
                        span, bucket = prepared
                status, error, code = "completed", "", None
                try:
                    response = await send(client, request, *args, **kwargs)
                    code = response.status_code
                    if code >= 400:
                        status, error = "error", f"HTTP {code}"
                    return response
                except asyncio.CancelledError:
                    status = "cancelled"
                    raise
                except Exception as exc:
                    status, error = "error", error_text(exc)
                    raise
                finally:
                    self.record("finish_attempt", span, status, error=error, http_status=code)
                    if bucket is not None:
                        bucket.update(error=error, finished=time.perf_counter())

            self._patch(runner_class, "_iter_llm_responses_with_fallback", wrapped_round)
            self._patch(runner_module, "AsyncRetrying", wrapped_factory)
            self._patch(request_module, "_build_retrying", wrapped_build)
            self._patch(http_class, "send", wrapped_send)
            self.status.update(framework=True, request=True, http=True, reason="installed")
            return True
        except Exception as exc:
            self.restore()
            self.status["reason"] = error_text(exc)
            return False

    def current_call(self):
        call = CALL.get()
        return call if call and call.get("owner") is self and call.get("active") else None

    def llm_metadata(self, runner):
        scope = ROUND.get()
        if scope and scope.get("runner") is runner and scope.get("owner") is self:
            return dict(round_id=scope["id"], **scope.get("attempt", {}))
        return {}

    def begin_call(self, runner, span):
        if span is None:
            return None
        scope = ROUND.get()
        if scope and scope.get("runner") is runner and scope.get("owner") is self:
            group = scope.get("group")
            if group is not None:
                group.last_span = span
        provider = getattr(runner, "provider", None)
        self.guard(lambda: self.ensure_provider(type(provider)))
        return dict(owner=self, span=span, provider=provider, active=True, provider_group=None)

    def _provider_start(self, provider):
        call = self.current_call()
        parent = PROVIDER.get()
        if not call or call["provider"] is not provider or (parent and parent.get("call") is call):
            return None
        group = call.get("provider_group")
        if not group or not group.get("error"):
            group = dict(id=uuid.uuid4().hex, number=0, error="", finished=None)
            call["provider_group"] = group
        group["number"] += 1
        retry = group["number"] > 1
        metadata = dict(
            retry_group_id=group["id"],
            attempt_number=group["number"],
            is_retry=retry,
            retry_reason=group["error"] if retry else "",
            retry_wait=max(0, time.perf_counter() - group["finished"]) if retry else 0,
            retry_wait_planned=None,
            wait_kind="recovery_gap",
        )
        span = self.record(
            "start_attempt", call, "provider", metadata, operation=type(provider).__name__
        )
        return dict(call=call, span=span, group=group)

    def ensure_provider(self, cls):
        if cls in self._provider_classes or cls.__module__ not in PROVIDER_MODULES:
            return
        # Both sync-response and streaming adapters use these boundaries in 4.28.
        for name in ("_query", "_query_stream"):
            original = getattr(cls, name, None)
            if original is None or getattr(original, "_astrbot_retry_monitor_wrapper", False):
                continue
            if inspect.iscoroutinefunction(original):
                wrapper = self._wrap_query(original)
            elif inspect.isasyncgenfunction(original):
                wrapper = self._wrap_query_stream(original)
            else:
                raise TypeError(f"Unsupported provider query target: {cls.__name__}.{name}")
            self._patch(cls, name, wrapper)
        self._provider_classes.add(cls)

    def _finish_provider(self, scope, status, error):
        if scope:
            self.record("finish_attempt", scope["span"], status, error=error)
            scope["group"].update(error=error, finished=time.perf_counter())

    def _wrap_query(self, original):
        @functools.wraps(original)
        async def wrapped(provider, *args, **kwargs):
            scope = self.guard(lambda: self._provider_start(provider))
            if scope is None:
                return await original(provider, *args, **kwargs)
            token = PROVIDER.set(scope)
            status, error = "completed", ""
            try:
                return await original(provider, *args, **kwargs)
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:
                status, error = "error", error_text(exc)
                raise
            finally:
                PROVIDER.reset(token)
                self._finish_provider(scope, status, error)

        return wrapped

    def _wrap_query_stream(self, original):
        @functools.wraps(original)
        async def wrapped(provider, *args, **kwargs):
            scope = self.guard(lambda: self._provider_start(provider))
            iterator = scoped_iterator(
                original(provider, *args, **kwargs), PROVIDER, scope or PROVIDER.get()
            )
            status, error = "interrupted", ""
            try:
                async for result in iterator:
                    # AstrBot can stop consuming immediately after the final response.
                    # Keep its terminal state even when finalization uses aclose().
                    if result is not None and not getattr(result, "is_chunk", False):
                        if getattr(result, "role", None) == "err":
                            status, error = (
                                "error",
                                str(getattr(result, "completion_text", "Provider error")),
                            )
                        elif status != "error":
                            status = "completed"
                    yield result
                if status != "error":
                    status = "completed"
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:
                status, error = "error", error_text(exc)
                raise
            finally:
                try:
                    await iterator.aclose()
                finally:
                    self._finish_provider(scope, status, error)

        return wrapped

    def _start_http(self, scope, request):
        # Ephemeral fingerprint only: never persist URLs, bodies, query strings or headers.
        fingerprint = hashlib.sha256(
            (request.method + " " + str(request.url.copy_with(query=None))).encode()
        ).hexdigest()
        bucket = scope["http"].get(fingerprint)
        header = request.headers.get("x-stainless-retry-count", "")
        number = int(header) + 1 if header.isdigit() else None
        retry = bool(
            (number is not None and number > 1)
            or (number is None and bucket and bucket.get("error"))
        )
        if bucket is None or not retry:
            bucket = dict(id=uuid.uuid4().hex, number=0, error="", finished=None)
            scope["http"][fingerprint] = bucket
        bucket["number"] = number if number is not None else bucket["number"] + 1
        metadata = dict(
            retry_group_id=bucket["id"],
            attempt_number=bucket["number"],
            is_retry=retry,
            retry_reason=bucket["error"] or ("SDK retry header" if retry else ""),
            retry_wait=max(0, time.perf_counter() - bucket["finished"])
            if retry and bucket["finished"]
            else 0,
            retry_wait_planned=None,
            wait_kind="sdk_gap",
        )
        span = self.record(
            "start_attempt",
            scope["call"],
            "http",
            metadata,
            parent_id=scope["span"]["id"] if scope["span"] else None,
            operation=request.method,
        )
        return span, bucket

    def snapshot(self):
        status = dict(self.status)
        owned = all(
            inspect.getattr_static(target, name, None) is wrapper
            for target, name, _, wrapper, _ in self._patches
        )
        status["ok"] = bool(
            self._patches and owned and all(status[key] for key in ("framework", "request", "http"))
        )
        status["provider_classes"] = sorted(cls.__name__ for cls in self._provider_classes)
        if self._patches and not owned:
            status["reason"] = "A retry target was replaced after installation"
        return status

    def restore(self):
        for target, name, original, wrapper, local in reversed(self._patches):
            if inspect.getattr_static(target, name, None) is wrapper:
                if local:
                    setattr(target, name, original)
                else:
                    delattr(target, name)
        self._patches.clear()
        self._provider_classes.clear()
        self.status.update(framework=False, request=False, http=False, reason="uninstalled")

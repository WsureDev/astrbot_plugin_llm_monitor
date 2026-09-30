"""Track image-result delivery using its exact source event, never a recent sender.

The image plugin deliberately breaks its Agent loop after a successful send.
Observe the owning coroutine so that closing that iterator is not a failure.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from contextvars import ContextVar

ORIGIN = ContextVar("llm_monitor_continuation", default=None)
IDENTITY_EXTRA = "astrbot_plugin_llm_monitor.caller_identity"
CONTINUATION_EXTRA = "astrbot_plugin_llm_monitor.continuation"


class ContinuationProbe:
    def __init__(self, plugin):
        self.plugin = plugin
        self.patches = []
        self.active = []
        self.reason = "Image generation plugin not loaded"

    def guard(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            self.plugin.record_failure(exc)
            return None

    def _patch(self, target, name, wrapper):
        original = inspect.getattr_static(target, name)
        wrapper._llm_monitor_continuation = self
        setattr(target, name, wrapper)
        self.patches.append((target, name, original, wrapper))

    def discover(self):
        """Called on initialization and AstrBot's plugin-loaded hook (hot reload too)."""
        getter = getattr(self.plugin.context, "get_registered_star", None)
        metadata = getter("astrbot_plugin_image_generation") if getter else None
        instance = getattr(metadata, "star_cls", None)
        handler = getattr(instance, "llm_result_handler", None)
        if handler is None:
            return
        from astrbot.core.cron.events import CronMessageEvent
        from astrbot.core.star.context import Context

        self.install(type(handler), CronMessageEvent, Context)

    def install(self, handler_class, event_class, context_class):
        method = handler_class.wake_ai_for_generation_task_result
        if getattr(method, "_llm_monitor_continuation", None) is self:
            return
        if not inspect.iscoroutinefunction(method) or not {"source_event", "task_id"}.issubset(
            inspect.signature(method).parameters
        ):
            self.reason = "Unsupported image-result handler signature"
            raise TypeError(self.reason)

        @functools.wraps(method)
        async def wrapped_wakeup(handler, *, task_id, source_event):
            if not self.plugin.capture_enabled(source_event):
                return await method(handler, task_id=task_id, source_event=source_event)
            scope = dict(
                owner=self,
                source=source_event,
                events=[],
                sent=0,
                failed=0,
                active=True,
                task_id=str(task_id),
                error="",
            )
            token = ORIGIN.set(scope)
            self.active.append(scope)
            status = "completed"
            try:
                return await method(handler, task_id=task_id, source_event=source_event)
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:
                status = "error"
                scope["error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                scope["active"] = False
                self.guard(self.plugin.finish_continuation, scope, status)
                ORIGIN.reset(token)
                self.active.remove(scope)

        self._patch(handler_class, "wake_ai_for_generation_task_result", wrapped_wakeup)
        if not any(target is event_class for target, *_ in self.patches):
            init = event_class.__init__

            @functools.wraps(init)
            def wrapped_init(event, *args, **kwargs):
                init(event, *args, **kwargs)
                scope = ORIGIN.get()
                if scope and scope["owner"] is self and scope["active"]:
                    self.guard(self._bind, scope, event)

            self._patch(event_class, "__init__", wrapped_init)
        if not any(target is context_class for target, *_ in self.patches):
            send = context_class.send_message

            @functools.wraps(send)
            async def wrapped_send(context, session, message_chain):
                scope = ORIGIN.get()
                tracked = bool(
                    scope
                    and scope["owner"] is self
                    and scope["active"]
                    and str(session) == str(scope["source"].unified_msg_origin)
                )
                try:
                    result = await send(context, session, message_chain)
                except Exception as exc:
                    if tracked:
                        scope["failed"] += 1
                        scope["error"] = f"{type(exc).__name__}: {exc}"
                    raise
                if tracked:
                    if result is True:
                        scope["sent"] += 1
                    elif result is False:
                        scope["failed"] += 1
                        scope["error"] = "AstrBot could not find a delivery platform"
                return result

            self._patch(context_class, "send_message", wrapped_send)
        self.reason = "Installed; source event and delivery result are authoritative"

    def _bind(self, scope, event):
        source = scope["source"]
        if str(source.unified_msg_origin) != str(event.unified_msg_origin):
            return
        identity = source.get_extra(IDENTITY_EXTRA)
        if not isinstance(identity, dict):
            identity = {
                key: getattr(source, "get_" + key)()
                for key in (
                    "platform_name",
                    "platform_id",
                    "message_type",
                    "sender_id",
                    "sender_name",
                    "group_id",
                )
            }
            identity["message_type"] = str(identity["message_type"])
        event.set_extra(IDENTITY_EXTRA, dict(identity))
        event.set_extra(CONTINUATION_EXTRA, scope)
        scope["events"].append(event)

    def snapshot(self):
        owned = bool(self.patches) and all(
            inspect.getattr_static(t, n) is w for t, n, _, w in self.patches
        )
        return dict(installed=owned, reason=self.reason, active=len(self.active))

    def restore(self):
        for target, name, original, wrapper in reversed(self.patches):
            if inspect.getattr_static(target, name) is wrapper:
                setattr(target, name, original)
        self.patches.clear()

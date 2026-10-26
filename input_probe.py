"""Snapshot the actual runner-to-Provider call without changing its arguments."""

from __future__ import annotations

import functools
import inspect

from .retry_probe import CALL
from .serialization import safe_json
from .wrapping import active_owned_wrapper, contains_wrapper


class ProviderInputProbe:
    def __init__(self, probe):
        self.probe = probe
        self._patches = []
        self._classes = set()
        self._generation = None

    @property
    def active(self):
        return self.probe.active and self._generation is not None

    def ensure_provider(self, cls):
        if cls in self._classes:
            return
        if self._generation is None:
            self._generation = object()
        for name in ("text_chat", "text_chat_stream"):
            original = getattr(cls, name, None)
            if original is None:
                continue
            if active_owned_wrapper(
                original, "_astrbot_input_wrapper", "_astrbot_input_owner", "ProviderInputProbe"
            ):
                continue
            if not (inspect.iscoroutinefunction(original) or inspect.isasyncgenfunction(original)):
                raise TypeError(f"Unsupported Provider input target: {cls.__name__}.{name}")
            wrapper = self._wrap(original, name)
            wrapper._astrbot_input_wrapper = True
            wrapper._astrbot_input_owner = self
            wrapper._astrbot_monitor_generation = self._generation
            local = name in vars(cls)
            setattr(cls, name, wrapper)
            self._patches.append((cls, name, original, wrapper, local))
        self._classes.add(cls)

    def _capture(self, signature, method, provider, args, kwargs, generation):
        call = CALL.get()
        if (
            not self.active
            or self._generation is not generation
            or not call
            or call.get("owner") is not self.probe.retry
            or not call.get("active")
            or call.get("provider") is not provider
            or call.get("input_captured")
        ):
            return
        # Inherited implementations and super() calls share the first snapshot.
        call["input_captured"] = True
        bound = signature.bind(provider, *args, **kwargs)
        payload = {}
        for key, value in list(bound.arguments.items())[1:]:
            if signature.parameters[key].kind == inspect.Parameter.VAR_KEYWORD:
                payload.update(value)
            else:
                payload[key] = value
        payload.pop("abort_signal", None)  # Runtime synchronization, not model content.
        tools = payload.pop("func_tool", None)
        snapshot = dict(
            capture_source="provider_call",
            provider_method=method,
            tool_schema_format="openai",
            model=call["span"]["provider_model"],
            messages=payload.pop("contexts", []),
            tools=tools.openai_schema() if tools is not None else [],
            parameters=payload,
        )
        # Freeze before the Provider can mutate messages or tool schemas.
        value = safe_json(snapshot, redact=self.probe.plugin.store.redact_secrets)
        self.probe._record("capture_llm_input", call["span"], value)

    def _wrap(self, original, method):
        signature = inspect.signature(original)
        generation = self._generation

        def capture(provider, args, kwargs):
            try:
                self._capture(signature, method, provider, args, kwargs, generation)
            except Exception as exc:
                self.probe.plugin.record_failure(exc)

        if inspect.isasyncgenfunction(original):

            @functools.wraps(original)
            async def wrapped(provider, *args, **kwargs):
                capture(provider, args, kwargs)
                iterator = original(provider, *args, **kwargs)
                try:
                    async for response in iterator:
                        yield response
                finally:
                    await iterator.aclose()

        else:

            @functools.wraps(original)
            async def wrapped(provider, *args, **kwargs):
                capture(provider, args, kwargs)
                return await original(provider, *args, **kwargs)

        return wrapped

    def intact(self):
        return all(
            contains_wrapper(inspect.getattr_static(cls, name, None), wrapper)
            for cls, name, _, wrapper, _ in self._patches
        )

    def restore(self):
        self._generation = None
        for cls, name, original, wrapper, local in reversed(self._patches):
            if inspect.getattr_static(cls, name, None) is wrapper:
                if local:
                    setattr(cls, name, original)
                else:
                    delattr(cls, name)
        self._patches.clear()
        self._classes.clear()

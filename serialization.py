"""Complete JSON serialization with credential redaction and cycle detection."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from itertools import chain
from typing import Any

SECRET_NAMES = r"api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|secret|password|passwd|token|cookie|set-cookie"
SECRET_KEY = re.compile(rf"^(?:{SECRET_NAMES})$", re.IGNORECASE)
QUOTED_SECRET = re.compile(
    rf"(?P<prefix>[\"'](?:{SECRET_NAMES})[\"']\s*:\s*)(?:\"(?:\\.|[^\"\\])*(?:\"|$)|'(?:\\.|[^'\\])*(?:'|$))",
    re.IGNORECASE,
)
ASSIGNED_SECRET = re.compile(
    rf"(?P<prefix>\b(?:{SECRET_NAMES})\b\s*[:=]\s*)(?:Bearer\s+)?[^\s,;\"'&<>}}]+",
    re.IGNORECASE,
)
BEARER = re.compile(r"\bBearer\s+[^\s\"'<>]+", re.IGNORECASE)


def redact_text(text: str, *, redact: bool = True) -> str:
    if redact:
        text = QUOTED_SECRET.sub(lambda m: m["prefix"] + '"[REDACTED]"', text)
        text = ASSIGNED_SECRET.sub(lambda m: m["prefix"] + "[REDACTED]", text)
        text = BEARER.sub("Bearer [REDACTED]", text)
    return text


def safe_json(value: Any, *, redact: bool = True) -> str:
    """Preserve all fields, items and text; scrub JSON embedded in MCP text too.

    Serialization failures propagate to the collector's error accounting rather
    than silently replacing the entire payload with an apparently valid record.
    """
    visited: set[int] = set()

    def walk(item: Any) -> Any:
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            if not redact:
                return item
            if item.strip()[:1] in ("{", "[", '"'):
                try:
                    decoded = json.loads(item)
                except (ValueError, RecursionError):
                    pass
                else:
                    return json.dumps(walk(decoded), ensure_ascii=False)
            return redact_text(item)
        if isinstance(item, (bytes, bytearray, memoryview)):
            return {"encoding": "base64", "data": base64.b64encode(item).decode("ascii")}
        if id(item) in visited:
            return "[circular reference]"
        visited.add(id(item))
        try:
            if isinstance(item, Mapping):
                entries = item.items()
            elif getattr(type(item), "model_fields", None):
                entries = chain(
                    ((key, getattr(item, key, None)) for key in type(item).model_fields),
                    (getattr(item, "model_extra", None) or {}).items(),
                )
            elif is_dataclass(item) and not isinstance(item, type):
                entries = ((field.name, getattr(item, field.name)) for field in fields(item))
            elif isinstance(item, (list, tuple, set, frozenset)):
                return [walk(child) for child in item]
            elif hasattr(item, "__dict__"):
                entries = vars(item).items()
            else:
                return redact_text(str(item), redact=redact)
            result = {}
            for key, child in entries:
                key = str(key)
                if redact and SECRET_KEY.fullmatch(key):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = walk(child)
            return result
        finally:
            visited.remove(id(item))

    return json.dumps(walk(value), ensure_ascii=False)

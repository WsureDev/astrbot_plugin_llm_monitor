"""Bounded, best-effort redaction for stored payloads and diagnostic messages."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from itertools import islice
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
TRUNCATED = "…[truncated]"


def redact_text(text: str, limit: int = 20000, redact: bool = True) -> str:
    # Bound work before regex parsing; unterminated quoted values are also scrubbed.
    cut = len(text) > limit
    text = text[:limit]
    if redact:
        text = QUOTED_SECRET.sub(lambda m: m["prefix"] + '"[REDACTED]"', text)
        text = ASSIGNED_SECRET.sub(lambda m: m["prefix"] + "[REDACTED]", text)
        text = BEARER.sub("Bearer [REDACTED]", text)
    return text + (TRUNCATED if cut else "")


def safe_json(value: Any, max_chars: int = 20000, redact: bool = True) -> str:
    """Sanitize fields and JSON carried inside MCP TextContent, with bounded work."""
    limit = max(1000, min(int(max_chars or 20000), 100000))
    remaining = limit * 2
    visited: set[int] = set()

    def walk(item: Any, depth: int = 0) -> Any:
        nonlocal remaining
        if remaining <= 0 or depth > 12:
            return TRUNCATED
        remaining -= 1
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            budget = max(0, min(remaining, limit))
            remaining -= min(len(item), budget)
            # Decode JSON only when the entire string is within the work budget.
            stripped = item.strip()
            if depth < 12 and len(item) <= budget and stripped[:1] in ("{", "[", '"'):
                try:
                    decoded = json.loads(item)
                except (ValueError, RecursionError):
                    pass
                else:
                    # Do not recursively consume the text budget twice.
                    remaining += min(len(item), budget)
                    return json.dumps(walk(decoded, depth + 1), ensure_ascii=False)
            return redact_text(item, budget, redact)
        if isinstance(item, (bytes, bytearray)):
            return f"[binary omitted: {len(item)} bytes]"
        if id(item) in visited:
            return "[circular reference]"
        visited.add(id(item))
        try:
            if isinstance(item, Mapping):
                fields = item.items()
            elif getattr(type(item), "model_fields", None):
                fields = ((key, getattr(item, key, None)) for key in type(item).model_fields)
            elif isinstance(item, (list, tuple, set)):
                result = []
                for child in islice(iter(item), 1000):
                    result.append(walk(child, depth + 1))
                    if remaining <= 0:
                        break
                if len(result) < len(item):
                    result.append(TRUNCATED)
                return result
            elif hasattr(item, "__dict__"):
                fields = vars(item).items()
            else:
                return f"[{type(item).__name__}]"
            result = {}
            for key, child in islice(fields, 1000):
                key = str(key)[:200]
                remaining -= len(key)
                if redact and SECRET_KEY.fullmatch(key):
                    result[key] = "[REDACTED]"
                elif key in {"data", "blob"} and isinstance(child, str) and len(child) > limit:
                    result[key] = f"[large data omitted: {len(child)} characters]"
                else:
                    result[key] = walk(child, depth + 1)
                if remaining <= 0:
                    result["_truncated"] = True
                    break
            return result
        finally:
            visited.discard(id(item))

    try:
        text = json.dumps(walk(value), ensure_ascii=False)
    except Exception:
        # A failing repr/model serializer must not expose the original payload.
        return '"[unserializable payload]"'
    return text if len(text) <= limit else text[:limit] + TRUNCATED

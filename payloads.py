"""Complete capture of response objects supplied by AstrBot."""

from __future__ import annotations

import json

from .serialization import safe_json


class ResponseCapture:
    """Accumulate all chunks and prefer the host's complete final response."""

    def __init__(self, *, redact=True):
        self.redact = redact
        self.parts = {"completion_text": [], "reasoning_content": []}
        self.chunks = []
        self.final = None

    def feed(self, response):
        get = (
            response.get
            if isinstance(response, dict)
            else lambda k, d=None: getattr(response, k, d)
        )
        if get("is_chunk", False):
            self.chunks.append(json.loads(safe_json(response, redact=self.redact)))
            for key in self.parts:
                text = get(key)
                if isinstance(text, str):
                    self.parts[key].append(text)
        else:
            self.final = json.loads(safe_json(response, redact=self.redact))
            self.chunks.clear()
            for parts in self.parts.values():
                parts.clear()

    def result(self):
        if self.final is not None:
            return self.final
        return {
            **{key: "".join(parts) for key, parts in self.parts.items()},
            "partial": True,
            "chunks": self.chunks,
        }

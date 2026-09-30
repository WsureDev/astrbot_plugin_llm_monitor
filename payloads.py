"""Bounded capture of input/output objects supplied by AstrBot."""

from __future__ import annotations


class ResponseCapture:
    """Bound streaming memory and prefer the host's final response over chunks."""

    def __init__(self, limit):
        self.limit = limit
        self.parts = {"completion_text": "", "reasoning_content": ""}
        self.final = None

    def feed(self, response):
        get = (
            response.get
            if isinstance(response, dict)
            else lambda k, d=None: getattr(response, k, d)
        )
        if get("is_chunk", False):
            for key in self.parts:
                text = get(key)
                if isinstance(text, str):
                    self.parts[key] += text[: max(0, self.limit - len(self.parts[key]))]
        else:
            self.final = {
                key: get(key)
                for key in (
                    "role",
                    "completion_text",
                    "reasoning_content",
                    "tools_call_name",
                    "tools_call_args",
                    "tools_call_ids",
                    "result_chain",
                )
                if get(key) is not None
            }

    def result(self):
        return self.final if self.final is not None else dict(self.parts, partial=True)

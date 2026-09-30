"""Optional non-streaming text cleanup, independent of telemetry collection."""

import re

THINKING_BLOCK = re.compile(r"<(thinking|think)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)


def strip_thinking(text: str) -> str:
    cleaned = THINKING_BLOCK.sub("", text)
    return cleaned.strip() if cleaned != text else text

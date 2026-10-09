"""Task identity belongs to an Agent runner, not its shared platform event."""

from contextvars import ContextVar

RUN_SCOPE = ContextVar("llm_monitor_runner_scope", default=None)

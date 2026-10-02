"""What one ``pydantic_ai`` turn records: the run it belongs to, what it billed, and the tools it called.

Split out of :mod:`teatree.agents.pydantic_ai_session` (module-health LOC cap); the session drives the turn,
this module owns the records it reports.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Any, TypedDict

from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.usage import RunUsage

from teatree.llm.anthropic_limits import EgressBlockedError
from teatree.llm.usage_tee import ROUTER_COST_KEYS, RequestUsage, UsageTee


class ToolCallEntry(TypedDict):
    """One tool call as persisted in ``TaskAttempt.result["tool_calls"]`` — its shape, never an argument value."""

    tool: str
    arg_keys: list[str]
    args_bytes: int
    output_bytes: int | None
    duration_ms: int | None


def router_reported_cost(run_usage: object) -> float | None:
    """The metered router's OWN reported cost from a pydantic_ai run usage, or ``None`` (#3157 E5).

    A metered OpenAI-compatible endpoint knows the real per-request cost; core only
    estimates it. When pydantic_ai surfaces that figure in ``RunUsage.details``, record THAT
    number (flagged not-estimated) instead of the price-table estimate. Absent (the common case
    today) → ``None``, so the estimate is used and flagged as such. Best-effort: any cost-like
    key, coerced to a non-negative float.
    """
    details = getattr(run_usage, "details", None)
    if not isinstance(details, dict):
        return None
    for key in ROUTER_COST_KEYS:
        value = details.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return None


def retry_text(part: RetryPromptPart) -> str:
    """The refusal text of a ``RetryPromptPart`` (a gate deny), as a plain string."""
    content = part.content
    return content if isinstance(content, str) else str(content)


@dataclass(slots=True)
class ToolCallRecord:
    """One tool call's trajectory entry; output and duration stay ``None`` for a call that never returned."""

    tool_call_id: str
    tool: str
    arg_keys: list[str]
    args_bytes: int
    started_at: float
    output_bytes: int | None = None
    duration_ms: int | None = None

    @classmethod
    def start(cls, part: ToolCallPart) -> "ToolCallRecord":
        return cls(
            tool_call_id=part.tool_call_id,
            tool=part.tool_name,
            arg_keys=list(part.args_as_dict()),
            args_bytes=len(part.args_as_json_str().encode()),
            started_at=time.monotonic(),
        )

    @property
    def returned(self) -> bool:
        return self.output_bytes is not None

    def finish(self, part: "ToolReturnPart | RetryPromptPart") -> None:
        output = part.model_response_str() if isinstance(part, ToolReturnPart) else retry_text(part)
        self.output_bytes = len(output.encode())
        self.duration_ms = int((time.monotonic() - self.started_at) * 1000)

    def as_record(self) -> ToolCallEntry:
        return {
            "tool": self.tool,
            "arg_keys": list(self.arg_keys),
            "args_bytes": self.args_bytes,
            "output_bytes": self.output_bytes,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class SessionRun:
    """One run of a session: the id the router keys it on, its request cap, and the tee on its transport.

    ``request_limit`` is the per-run sequential-request cap (the metered-lane guardrail); ``None`` or
    ``<= 0`` leaves the run uncapped (the ``claude_sdk`` behaviour).
    """

    session_id: str
    usage_tee: UsageTee
    request_limit: int | None = None

    @classmethod
    def start(cls, *, request_limit: int | None = None) -> "SessionRun":
        return cls(session_id=uuid.uuid4().hex, usage_tee=UsageTee(), request_limit=request_limit)


@dataclass(frozen=True, slots=True)
class TurnSpend:
    """What one turn billed: the router's own cost, the token usage, and the model it says ran."""

    cost_usd: float | None
    usage: dict[str, Any]
    model: str

    @classmethod
    def measure(
        cls,
        run_usage: RunUsage,
        requests: list[RequestUsage],
        *,
        requested_model: str,
        trajectory: list[ToolCallEntry],
    ) -> "TurnSpend":
        usage: dict[str, Any] = {
            "input_tokens": run_usage.input_tokens,
            "output_tokens": run_usage.output_tokens,
            "cache_read_input_tokens": run_usage.cache_read_tokens,
            "cache_creation_input_tokens": run_usage.cache_write_tokens,
        }
        if requests:
            usage["per_request"] = [request.as_record() for request in requests]
        if trajectory:
            usage["tool_calls"] = trajectory
        costs = [cost for request in requests if (cost := request.cost_usd) is not None]
        resolved = [request.resolved_model for request in requests if request.resolved_model]
        return cls(
            cost_usd=sum(costs) if costs else router_reported_cost(run_usage),
            usage=usage,
            model=resolved[-1] if resolved else requested_model,
        )


def egress_block_in(exc: BaseException) -> EgressBlockedError | None:
    """The egress refusal in *exc*'s chain, whether raised directly or wrapped as a connection failure."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, EgressBlockedError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None

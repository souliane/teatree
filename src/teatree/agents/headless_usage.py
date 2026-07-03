"""Cost / token-usage accounting for the headless agent runner.

Maps a ``claude-agent-sdk`` :class:`~claude_agent_sdk.ResultMessage` to the
``AttemptUsage`` the attempt recorder persists: token counts, the billed model,
and the cost (SDK-reported when present, else the price-table estimate). Split out
of ``agents/headless.py`` so the run/dispatch logic and this accounting concern
each stay a focused module.
"""

from typing import TYPE_CHECKING, Any

from claude_agent_sdk import ResultMessage

if TYPE_CHECKING:
    from teatree.agents.attempt_recorder import AttemptUsage


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))  # ty: ignore[invalid-argument-type]
    except (ValueError, TypeError):
        return None


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # ty: ignore[invalid-argument-type]
    except (ValueError, TypeError):
        return None


def _attempt_usage(message: ResultMessage | None, *, lane: str = "") -> "AttemptUsage":
    """Map a :class:`~claude_agent_sdk.ResultMessage` to ``AttemptUsage``.

    Token counts come from the nested ``usage`` dict (``input_tokens`` /
    ``output_tokens`` / ``cache_creation_input_tokens`` /
    ``cache_read_input_tokens``), the billed model from the single key of
    ``model_usage`` (e.g. ``claude-opus-4-8[1m]``), the cost from
    ``total_cost_usd`` (else the price-table estimate). *lane* is the resolved
    Layer-2 lane (souliane/teatree#657) this dispatch authenticated through —
    independent of the message, so it is stamped even when *message* is
    ``None``.
    """
    from teatree.agents.attempt_recorder import AttemptUsage  # noqa: PLC0415

    if message is None:
        return AttemptUsage(lane=lane)
    usage = message.usage if isinstance(message.usage, dict) else {}
    model = _billed_model(message.model_usage)
    return AttemptUsage(
        agent_session_id=message.session_id or "",
        model=model,
        input_tokens=_safe_int(usage.get("input_tokens")),
        output_tokens=_safe_int(usage.get("output_tokens")),
        cache_read_tokens=_safe_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_safe_int(usage.get("cache_creation_input_tokens")),
        cost_usd=_resolve_cost_usd(message, usage=usage, model=model),
        num_turns=message.num_turns,
        lane=lane,
    )


def _billed_model(model_usage: dict[str, Any] | None) -> str:
    """Return the billed model id from ``model_usage`` (single-model run), or ``""``.

    ``model_usage`` is the SDK's untyped ``ResultMessage.model_usage`` dict.
    """
    if isinstance(model_usage, dict) and model_usage:
        return str(next(iter(model_usage)))
    return ""


def _resolve_cost_usd(message: ResultMessage, *, usage: dict[str, Any], model: str) -> float | None:
    """Persist the SDK-reported cost when present, else the price-table estimate.

    Persisting an estimate at capture time means a row's ``cost_usd`` is never
    NULL once any token count was captured — the ``t3 cost`` report and the
    watchdog both read a real number rather than re-deriving it each query.
    Returns ``None`` only when nothing at all was captured.
    """
    reported = _safe_float(message.total_cost_usd)
    if reported is not None:
        return reported
    token_keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    if all(usage.get(key) is None for key in token_keys):
        return None
    from teatree.core.cost import AttemptUsage, price_table_cost_usd  # noqa: PLC0415

    return price_table_cost_usd(
        AttemptUsage(
            model=model or None,
            reported_cost_usd=None,
            input_tokens=_safe_int(usage.get("input_tokens")) or 0,
            output_tokens=_safe_int(usage.get("output_tokens")) or 0,
            cache_read_tokens=_safe_int(usage.get("cache_read_input_tokens")) or 0,
            cache_write_tokens=_safe_int(usage.get("cache_creation_input_tokens")) or 0,
        ),
    )

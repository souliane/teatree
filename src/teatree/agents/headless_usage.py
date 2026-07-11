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
    cost_usd, estimated = _resolve_cost_usd(message, usage=usage, model=model)
    return AttemptUsage(
        agent_session_id=message.session_id or "",
        model=model,
        input_tokens=_safe_int(usage.get("input_tokens")),
        output_tokens=_safe_int(usage.get("output_tokens")),
        cache_read_tokens=_safe_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_safe_int(usage.get("cache_creation_input_tokens")),
        cost_usd=cost_usd,
        num_turns=message.num_turns,
        lane=lane,
        cost_is_estimated=estimated,
    )


def _billed_model(model_usage: dict[str, Any] | None) -> str:
    """Return the billed model id from ``model_usage`` (single-model run), or ``""``.

    ``model_usage`` is the SDK's untyped ``ResultMessage.model_usage`` dict.
    """
    if isinstance(model_usage, dict) and model_usage:
        return str(next(iter(model_usage)))
    return ""


def _resolve_cost_usd(message: ResultMessage, *, usage: dict[str, Any], model: str) -> tuple[float | None, bool]:
    """Return ``(cost_usd, is_estimated)`` — the reported figure when present, else the estimate.

    The reported ``total_cost_usd`` — the CLI/SDK figure, OR the metered router's own
    reported cost passed through onto the terminal ``ResultMessage`` by
    :class:`~teatree.agents.harness.PydanticAiHarnessSession` (#3157 E5) — is preferred and
    flagged NOT estimated (``is_estimated=False``). Only when no reported figure exists does
    it fall back to the price-table estimate (``is_estimated=True``), so ``t3 cost`` can
    distinguish a router-lane run's real cost from a price-table guess. Returns
    ``(None, True)`` when nothing at all was captured.
    """
    reported = _safe_float(message.total_cost_usd)
    if reported is not None:
        return reported, False
    token_keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    if all(usage.get(key) is None for key in token_keys):
        return None, True
    from teatree.core.cost import AttemptUsage, price_table_cost_usd  # noqa: PLC0415

    estimate = price_table_cost_usd(
        AttemptUsage(
            model=model or None,
            reported_cost_usd=None,
            input_tokens=_safe_int(usage.get("input_tokens")) or 0,
            output_tokens=_safe_int(usage.get("output_tokens")) or 0,
            cache_read_tokens=_safe_int(usage.get("cache_read_input_tokens")) or 0,
            cache_write_tokens=_safe_int(usage.get("cache_creation_input_tokens")) or 0,
        ),
    )
    return estimate, True

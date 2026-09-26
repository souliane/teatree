"""Cost / token-usage accounting for the headless agent runner.

Maps a ``claude-agent-sdk`` :class:`~claude_agent_sdk.ResultMessage` to the
``AttemptUsage`` the attempt recorder persists: token counts, the billed model,
and the cost (SDK-reported when present, else the price-table estimate). Split out
of ``agents/runner.py`` so the run/dispatch logic and this accounting concern
each stay a focused module.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from claude_agent_sdk import ResultMessage

from teatree.agents.skill_assurance import SkillAssurance

if TYPE_CHECKING:
    from teatree.agents.attempt_recorder import AttemptUsage
    from teatree.agents.pydantic_ai_turn import ToolCallEntry
    from teatree.llm.usage_tee import RequestRecord

EffortResolver = Callable[..., str | None]


def resolve_provenance_effort(resolver: EffortResolver, phase: str, harness_name: str) -> str:
    """Resolve effort while preserving the original Claude call contract."""
    if harness_name == "claude_sdk":
        return resolver(phase) or ""
    return resolver(phase, harness=harness_name) or ""


@dataclass(frozen=True, slots=True)
class DispatchProvenance:
    """The dispatch-time pins (#3673 Tier 3) stamped onto a recorded attempt.

    Resolved before the run — independent of the harness outcome — so the drawer
    shows the reasoning effort and skill bundle the dispatch actually ran with.
    """

    reasoning_effort: str = ""
    skills_loaded: tuple[str, ...] = ()
    skill_assurance: SkillAssurance | None = None
    selected_harness: str = ""
    selected_provider: str = ""
    selected_model: str = ""
    route_candidate_index: int | None = None
    route_source_skill: str = ""
    fallback_reason: str = ""
    fallback_from_attempt_id: int | None = None


@dataclass(frozen=True, slots=True)
class UsageObservation:
    lane: str = ""
    reasoning_effort: str = ""
    skills_loaded: tuple[str, ...] = ()
    tool_calls: int | None = None
    provenance: DispatchProvenance = field(default_factory=DispatchProvenance)


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


def _attempt_usage(message: ResultMessage | None, observation: UsageObservation | None = None) -> "AttemptUsage":
    """Map a :class:`~claude_agent_sdk.ResultMessage` to ``AttemptUsage``.

    Token counts come from the nested ``usage`` dict (``input_tokens`` /
    ``output_tokens`` / ``cache_creation_input_tokens`` /
    ``cache_read_input_tokens``), the billed model from the single key of
    ``model_usage`` (a dated id, optionally ``[1m]``-suffixed), the cost from
    ``total_cost_usd`` (else the price-table estimate). The dispatch observation
    carries lane, reasoning effort, skills and tool calls measured by the driver
    rather than the message (souliane/teatree#657, #3673), so they are stamped
    even when *message* is ``None`` — for tool calls that
    matters: a run whose stream died before its terminal message still measured
    its own (possibly zero) tool count, and losing it here would degrade the
    :mod:`teatree.agents.action_verification` gate to UNMEASURED exactly when it
    is most needed.
    """
    from teatree.agents.attempt_recorder import AttemptUsage  # noqa: PLC0415 — deferred: call-time import, kept lazy

    observation = observation or UsageObservation()
    skills = list(observation.skills_loaded)
    provenance = observation.provenance
    if message is None:
        return AttemptUsage(
            lane=observation.lane,
            usage_unknown=True,
            reasoning_effort=observation.reasoning_effort,
            skills_loaded=skills,
            skill_assurance=provenance.skill_assurance,
            tool_calls=observation.tool_calls,
            selected_harness=provenance.selected_harness,
            selected_provider=provenance.selected_provider,
            selected_model=provenance.selected_model,
            route_candidate_index=provenance.route_candidate_index,
            route_source_skill=provenance.route_source_skill,
            fallback_reason=provenance.fallback_reason,
            fallback_from_attempt_id=provenance.fallback_from_attempt_id,
        )
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
        lane=observation.lane,
        cost_is_estimated=estimated,
        usage_unknown=_spend_is_unknown(message, usage=usage),
        reasoning_effort=observation.reasoning_effort,
        skills_loaded=skills,
        skill_assurance=provenance.skill_assurance,
        tool_calls=observation.tool_calls,
        usage_per_request=cast("list[RequestRecord]", _records(usage.get("per_request"))),
        trajectory=cast("list[ToolCallEntry]", _records(usage.get("tool_calls"))),
        selected_harness=provenance.selected_harness,
        selected_provider=provenance.selected_provider,
        selected_model=provenance.selected_model,
        route_candidate_index=provenance.route_candidate_index,
        route_source_skill=provenance.route_source_skill,
        fallback_reason=provenance.fallback_reason,
        fallback_from_attempt_id=provenance.fallback_from_attempt_id,
    )


def _records(value: object) -> list[Mapping[str, object]]:
    """A list of records the transport left on the usage map under one key, or none."""
    if not isinstance(value, list):
        return []
    return [record for record in value if isinstance(record, dict)]


#: The ``ResultMessage.usage`` keys a provider reports its token counts under. Absent
#: from ALL of them is the only shape in which a token count is genuinely unreported.
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _spend_is_unknown(message: ResultMessage, *, usage: dict[str, Any]) -> bool:
    """Whether turns RAN and the provider still reported no tokens at all (#4816).

    A reported ``0`` is a measurement and stays one; the unknown is the absence. A run
    with no turns billed nothing, which is a different — and already representable — state.
    """
    return (message.num_turns or 0) > 0 and all(usage.get(key) is None for key in _TOKEN_KEYS)


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
    if all(usage.get(key) is None for key in _TOKEN_KEYS):
        return None, True
    from teatree.core.cost import AttemptUsage, price_table_cost_usd  # noqa: PLC0415 — deferred: call-time import

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

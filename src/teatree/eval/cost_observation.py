"""What one eval run cost, and whether that figure was measured at all.

Only the CLI-backed ``api`` lane reports a cost of its own. Every ``PydanticAiRunner``
lane — ``anthropic_api`` (the backend CI runs) and ``pydantic_ai`` — carries
``total_cost_usd=None``, so reading the transport's figure alone floors a spending run
to ``$0.00``. The same terminal message DOES carry the four billed token counts and the
billed model's id, and Anthropic bills those at list price, so the figure is computable
rather than absent.

Three outcomes, and the third is the point: a run whose cost cannot be established
reports :data:`~teatree.eval.models.COST_SOURCE_UNKNOWN`, never ``$0.00``. A measured
zero and an absent measurement render identically otherwise, and the resulting
``no metered calls`` line is the reassurance that would stop someone noticing runaway
spend on a metered key. That is why the price lookup here is
:meth:`~teatree.core.cost.ModelPrice.known_for` rather than
:func:`~teatree.core.cost.price_for_model`: a swapped non-Claude model with no configured
rate has to read *unknown*, not a confidently-wrong figure at the opus fallback rate.
"""

from dataclasses import dataclass

from teatree.core.cost import ModelPrice
from teatree.eval.models import COST_SOURCE_DERIVED, COST_SOURCE_REPORTED, COST_SOURCE_UNKNOWN
from teatree.eval.transcript import StreamJsonEvent, extract_billed_model, extract_usage, reported_cost_usd


@dataclass(frozen=True, slots=True)
class CostObservation:
    """One run's dollar figure paired with how it was obtained."""

    usd: float
    source: str


UNKNOWN_COST = CostObservation(usd=0.0, source=COST_SOURCE_UNKNOWN)


def observe_cost(
    events: list[StreamJsonEvent], *, requested_model: str = "", price_from_usage: bool = True
) -> CostObservation:
    """Price one run's trajectory: the transport's own figure, else its token usage.

    *requested_model* is the fallback pricing key for a transport that reports usage but
    no ``model_usage`` map; it is the model the scenario asked for, which is the one that
    billed unless a fallback kicked in (and a fallback populates ``model_usage``, so the
    observed key wins whenever it exists).

    *price_from_usage* is False for the one transport that reports its OWN bill: there,
    silence means it billed nothing, so deriving a figure would contradict the only
    authority on the question. Fixed per transport — never a configurable knob.
    """
    reported = reported_cost_usd(events)
    if reported is not None:
        return CostObservation(usd=reported, source=COST_SOURCE_REPORTED)
    if not price_from_usage:
        return UNKNOWN_COST
    usage = extract_usage(events)
    if usage.total_input == 0 and usage.output == 0:
        return UNKNOWN_COST
    price = ModelPrice.known_for(extract_billed_model(events) or requested_model)
    if price is None:
        return UNKNOWN_COST
    derived = price.cost(
        input_tokens=usage.input,
        output_tokens=usage.output,
        cache_read_tokens=usage.cache_read,
        cache_write_tokens=usage.cache_creation,
    )
    return CostObservation(usd=derived, source=COST_SOURCE_DERIVED)


__all__ = ["UNKNOWN_COST", "CostObservation", "observe_cost"]

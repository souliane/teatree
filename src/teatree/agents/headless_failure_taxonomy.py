"""Terminal-``ResultMessage`` failure taxonomy for the headless runner.

The two pure classifiers the driver folds a non-success run through, factored out
of :mod:`teatree.agents.headless` so the driver keeps only the decision order and
this leaf owns what each terminal message MEANS: was the run stopped by a
model-access limit (:func:`limit_match`), or did it end in a genuine failure that
must be recorded rather than laundered into a completion (:func:`error_result_reason`)?

Both are pure functions of the SDK message, so the taxonomy is testable without a
task, a harness, or a database — and both lanes reach the same verdict for the same
message, which is what keeps the ``claude_sdk`` and ``pydantic_ai`` failure
vocabularies from drifting apart.
"""

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import RateLimitInfo

from teatree.llm.anthropic_limits import LimitMatch, classify_limit, classify_rate_limit_type

#: Prefix stamped on a genuine FAILED run's recorded reason. It is ALSO a transient
#: marker (:mod:`teatree.agents.outage_classifier`), so the bounded auto-requeue
#: sweep reopens such a run — which is right for an interruption and wrong for a
#: deliberate ceiling, so a ceiling breach carries its own named reason instead.
RESULT_ERROR_PREFIX = "result_error: "


def error_result_reason(message: ResultMessage | None) -> str | None:
    """Return a failure reason when the run did NOT complete cleanly, else ``None``.

    A missing terminal ``ResultMessage`` (the stream ended before the CLI emitted
    one) and a ``ResultMessage(is_error=True)`` that is NOT a usage-limit message
    are both genuine FAILED runs (#1764 class): they must record a failed attempt
    carrying the CLI's own ``result`` / ``errors`` / ``api_error_status``, never
    be laundered into a completion that advances the ticket FSM over a failed run.
    Called only AFTER :func:`limit_match` has already claimed a limit error, so a
    limit message never reaches here.
    """
    if message is None:
        return f"{RESULT_ERROR_PREFIX}no terminal ResultMessage — the run ended without completing"
    if not message.is_error:
        return None
    detail = str(message.result or "").strip()
    if not detail and message.errors:
        detail = "; ".join(str(err) for err in message.errors)
    status = message.api_error_status
    parts = [f"subtype={message.subtype}"]
    if status:
        parts.append(f"api_error_status={status}")
    if detail:
        parts.append(detail)
    return RESULT_ERROR_PREFIX + " — ".join(parts)


def limit_match(message: ResultMessage | None, rate_limit_info: RateLimitInfo | None = None) -> LimitMatch | None:
    """Return the classified :class:`LimitMatch`, or ``None`` when not a limit error.

    Keyed on ``is_error`` so a healthy result whose text merely discusses limits
    is never flagged. When the run IS an error and the stream carried a rejected
    :class:`~claude_agent_sdk.types.RateLimitInfo`, classify from its TYPED
    ``rate_limit_type`` window (unambiguous structured data — a ``seven_day_opus``
    is the WEEKLY cause, never a 5-hour one); otherwise fall back to phrase-matching
    the agent's final ``result`` string. Either way
    :func:`~teatree.llm.anthropic_limits.classify_limit` sorts it into its distinct
    cause (API-credit / subscription-session / subscription-weekly / rate-limit),
    so a credit-empty key is never reported as a subscription quota.
    """
    if message is None or not message.is_error:
        return None
    if rate_limit_info is not None and rate_limit_info.status == "rejected":
        typed = classify_rate_limit_type(rate_limit_info.rate_limit_type)
        if typed is not None:
            return typed
    return classify_limit(str(message.result or ""))


__all__ = ["RESULT_ERROR_PREFIX", "error_result_reason", "limit_match"]

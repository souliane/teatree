"""Terminal-``ResultMessage`` failure taxonomy for the agent runner.

The pure classifiers the driver folds a non-success run through, factored out
of :mod:`teatree.agents.runner` so the driver keeps only the decision order and
this leaf owns what each terminal message MEANS: was the run stopped by a
model-access limit (:func:`limit_match`), did its context window fill
(:func:`is_context_exhaustion`), or did it end in a genuine failure that
must be recorded rather than laundered into a completion (:func:`error_result_reason`)?

Both are pure functions of the SDK message, so the taxonomy is testable without a
task, a harness, or a database — and both lanes reach the same verdict for the same
message, which is what keeps the ``claude_sdk`` and ``pydantic_ai`` failure
vocabularies from drifting apart.
"""

from http import HTTPStatus

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import RateLimitInfo

from teatree.core.modelkit.task_failure_taxonomy import CONTEXT_EXHAUSTED_MARKER, RESULT_ERROR_MARKER
from teatree.llm.anthropic_limits import (
    LimitCause,
    LimitMatch,
    classify_limit,
    classify_rate_limit_type,
    provider_budget_match,
)

#: Prefix stamped on a genuine FAILED run's recorded reason. It is ALSO a transient
#: marker (:mod:`teatree.agents.outage_classifier`), so the bounded auto-requeue
#: sweep reopens such a run — which is right for an interruption and wrong for a
#: deliberate ceiling, so a ceiling breach carries its own named reason instead.
RESULT_ERROR_PREFIX = f"{RESULT_ERROR_MARKER} "

#: The terminal ``ResultMessage`` subtype a run carries when it ended because it
#: reached its own per-run turn ceiling — emitted by the ``claude`` CLI for the
#: ``ClaudeAgentOptions.max_turns`` cap (``agent_max_turns``) and stamped by
#: :mod:`teatree.agents.pydantic_ai_session` for that lane's ``UsageLimits`` request
#: cap (``pydantic_ai_request_limit``). ONE subtype for one meaning, so both lanes
#: land in the same branch. It distinguishes "the run was cut off at its ceiling"
#: from every other failed result, so a cap is never mistaken for an ordinary error
#: — nor an ordinary error laundered into a cap.
TURN_CEILING_SUBTYPE = "error_max_turns"

#: How the API and the ``claude`` CLI word a request that no longer fits the model's context window.
CONTEXT_EXHAUSTION_PHRASES = (
    "prompt is too long",
    "input is too long for requested model",
    "input length and `max_tokens` exceed context limit",
)

#: The HTTP statuses on which a provider has REFUSED the credential outright — a router key
#: at its cycle spend limit, a revoked or wrong key. Unlike a 429 these never clear on their
#: own within a turn, and no Anthropic phrase names them, so the status is the only signal:
#: an OpenAI-compatible router's ``403 access_denied: token cycle spend limit reached``
#: matched nothing in the phrase table and failed 307 tasks one at a time (#4816).
HARD_REFUSAL_STATUSES = frozenset({401, 403})


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


def is_context_exhaustion(message: ResultMessage | None) -> bool:
    """Whether a failed run ended because its context window filled — how a long uncompacted run ends."""
    if message is None or not message.is_error:
        return False
    text = " ".join([str(message.result or ""), *(message.errors or [])]).casefold()
    return any(phrase in text for phrase in CONTEXT_EXHAUSTION_PHRASES)


def context_exhaustion_reason(message: ResultMessage | None) -> str:
    """The recorded reason for a full context window: a retried kind, so the run re-dispatches fresh."""
    detail = (error_result_reason(message) or "").removeprefix(RESULT_ERROR_PREFIX)
    return f"{RESULT_ERROR_PREFIX}{CONTEXT_EXHAUSTED_MARKER} — {detail}"


def limit_match(
    message: ResultMessage | None, rate_limit_info: RateLimitInfo | None = None, *, metered_transport: bool = False
) -> LimitMatch | None:
    """Return the classified :class:`LimitMatch`, or ``None`` when not a limit error.

    Keyed on ``is_error`` so a healthy result whose text merely discusses limits
    is never flagged. A run that ended at its OWN ceiling
    (:data:`TURN_CEILING_SUBTYPE`) is never a provider window on either lane, by
    that subtype's contract, so it short-circuits to ``None`` BEFORE any
    text matching: the vendor's ``UsageLimitExceeded`` message names its own docs
    ("see the docs on usage limits ..."), and phrase-matching that prose would
    sort a self-imposed request cap into ``SUBSCRIPTION_SESSION`` and PARK a run
    that must be recorded FAILED. Structured cause first, prose only as a
    fallback — the same order the typed ``rate_limit_type`` branch below uses.

    A hard :data:`HARD_REFUSAL_STATUSES` status is read NEXT, ahead of both the typed
    window and the prose: the provider has refused the credential, which is a fact about
    the transport rather than about any Anthropic window, and on a non-Anthropic router
    the body is not this vocabulary at all. A body that names a spend stop keeps the more
    specific provider-budget cause, whose park horizon is the longer one.

    When the run IS an error and the stream carried a rejected
    :class:`~claude_agent_sdk.types.RateLimitInfo`, classify from its TYPED
    ``rate_limit_type`` window (unambiguous structured data — a ``seven_day_opus``
    is the WEEKLY cause, never a 5-hour one); otherwise fall back to phrase-matching
    the agent's final ``result`` string. Either way
    :func:`~teatree.llm.anthropic_limits.classify_limit` sorts it into its distinct
    cause (API-credit / subscription-session / subscription-weekly / rate-limit / provider-budget),
    so a credit-empty key is never reported as a subscription quota.

    On a *metered_transport* (the ``pydantic_ai`` lane talking to the provider directly) the HTTP status
    then decides what prose cannot: a 402 is a provider budget and a 429 a rate limit even when the body
    carries no known wording. A body that names a spend stop stays the more specific budget cause. A
    ``claude_sdk`` result never takes that fallback: its status
    was not the metered router's, so it is classified by type and prose alone.
    """
    if message is None or not message.is_error:
        return None
    if message.subtype == TURN_CEILING_SUBTYPE:
        return None
    if message.api_error_status in HARD_REFUSAL_STATUSES:
        named = classify_limit(str(message.result or ""))
        if named is not None and named.cause is LimitCause.PROVIDER_BUDGET:
            return named
        return LimitMatch(phrase=f"http {message.api_error_status}", cause=LimitCause.PROVIDER_ACCESS_DENIED)
    if rate_limit_info is not None and rate_limit_info.status == "rejected":
        typed = classify_rate_limit_type(rate_limit_info.rate_limit_type)
        if typed is not None:
            return typed
    text = str(message.result or "")
    matched = classify_limit(text)
    return _metered_status_match(message.api_error_status, text, matched) if metered_transport else matched


def _metered_status_match(status: int | None, text: str, matched: LimitMatch | None) -> LimitMatch | None:
    """The metered router's HTTP status where its prose names no known limit, else *matched*."""
    names_a_budget = matched is not None and matched.cause is LimitCause.PROVIDER_BUDGET
    if status == HTTPStatus.PAYMENT_REQUIRED and not names_a_budget:
        return provider_budget_match("http 402", text)
    if status == HTTPStatus.TOO_MANY_REQUESTS and matched is None:
        return LimitMatch(phrase="http 429", cause=LimitCause.RATE_LIMIT)
    return matched


__all__ = [
    "CONTEXT_EXHAUSTION_PHRASES",
    "HARD_REFUSAL_STATUSES",
    "RESULT_ERROR_PREFIX",
    "TURN_CEILING_SUBTYPE",
    "context_exhaustion_reason",
    "error_result_reason",
    "is_context_exhaustion",
    "limit_match",
]

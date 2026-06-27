"""Classify an Anthropic exhaustion signal into its distinct, non-interchangeable cause.

A terminal run can halt for several distinct exhaustion reasons that demand
DIFFERENT remediations — conflating them sends the operator to the wrong fix.
The four causes (see :class:`LimitCause`):

- API-key CREDIT exhaustion — the billed ``ANTHROPIC_API_KEY`` has a $0 balance;
    a real ``/v1/messages`` call returns HTTP 400 (credit balance too low). Fix:
    add credits at console.anthropic.com. This is NOT a subscription limit and
    must never be reported as one — the metered-eval path rides this key.
- subscription SESSION limit — the ~5h rolling limit; resets the same day, so
    re-dispatching later works.
- subscription WEEKLY limit — the 7-day window; hard, resets weekly.
- transient API rate limit — HTTP 429; retry shortly.

The phrase set is the bundled ``claude`` CLI's own error vocabulary — its
auth-error classifier groups ``credit balance (?:is )?too low | usage limit
reached``, and the SDK types name the subscription windows ``five_hour`` /
``seven_day`` (see ``claude_agent_sdk.types.RateLimitType``). The matcher keys on
those REAL strings, never a guess, so a credit-empty condition is never laundered
into a subscription-quota report.
"""

import dataclasses
from enum import Enum


class LimitCause(Enum):
    """The distinct exhaustion cause a terminal limit signal carries.

    The ``value`` doubles as the machine marker prefixed onto a recorded failure
    reason, so a downstream reader can branch on the cause without re-parsing the
    human message.
    """

    API_CREDIT = "api_credit"
    SUBSCRIPTION_SESSION = "subscription_session"
    SUBSCRIPTION_WEEKLY = "subscription_weekly"
    RATE_LIMIT = "rate_limit"


#: Phrase -> cause, ordered MOST-SPECIFIC first. Credit phrases precede every
#: subscription phrase (their remediation is unrelated to any plan), and the
#: weekly phrase precedes the generic session ``usage limit`` so a 7-day message
#: is never mislabeled a 5-hour one. Each phrase is a substring the bundled
#: ``claude`` CLI actually emits — see the module docstring for provenance.
_SIGNATURES: tuple[tuple[str, LimitCause], ...] = (
    ("credit balance too low", LimitCause.API_CREDIT),
    ("credit balance is too low", LimitCause.API_CREDIT),
    ("out of credits", LimitCause.API_CREDIT),
    ("weekly limit", LimitCause.SUBSCRIPTION_WEEKLY),
    ("7-day limit", LimitCause.SUBSCRIPTION_WEEKLY),
    ("seven-day limit", LimitCause.SUBSCRIPTION_WEEKLY),
    ("5-hour limit", LimitCause.SUBSCRIPTION_SESSION),
    ("five-hour limit", LimitCause.SUBSCRIPTION_SESSION),
    ("session limit", LimitCause.SUBSCRIPTION_SESSION),
    ("usage limit", LimitCause.SUBSCRIPTION_SESSION),
    ("rate limit", LimitCause.RATE_LIMIT),
)

#: Operator-facing remediation per cause. The API-credit message names the
#: console explicitly and never says "subscription"; the two subscription
#: messages name their own reset cadence so session and weekly read distinctly.
_REMEDIATION: dict[LimitCause, str] = {
    LimitCause.API_CREDIT: (
        "API credits exhausted — the billed ANTHROPIC_API_KEY has no balance; add credits at console.anthropic.com"
    ),
    LimitCause.SUBSCRIPTION_SESSION: (
        "subscription session limit reached (the ~5h rolling limit) — retry after it resets (same day)"
    ),
    LimitCause.SUBSCRIPTION_WEEKLY: ("subscription weekly limit reached — retry after the weekly reset"),
    LimitCause.RATE_LIMIT: ("Anthropic API rate limit hit (transient) — retry shortly"),
}


@dataclasses.dataclass(frozen=True)
class LimitMatch:
    """A matched exhaustion signal: the phrase that fired and its classified cause."""

    phrase: str
    cause: LimitCause

    @property
    def remediation(self) -> str:
        """The operator-facing remediation message for this match's cause."""
        return _REMEDIATION[self.cause]

    def as_reason(self) -> str:
        """The recorded failure reason: ``<cause>: <phrase> — <remediation>``.

        The leading ``<cause>`` marker lets a downstream reader branch on the
        cause without re-parsing prose; the phrase preserves the actual signal
        the CLI emitted; the remediation tells the operator exactly what to do.
        """
        return f"{self.cause.value}: {self.phrase} — {self.remediation}"


def classify_limit(text: str) -> LimitMatch | None:
    """Return the :class:`LimitMatch` *text* signals, or ``None`` when it names no known limit.

    Case-insensitive substring match against :data:`_SIGNATURES` in order, so the
    most-specific phrase wins. The caller is responsible for gating on whatever
    "this is an error" signal its transport carries (``ResultMessage.is_error``
    for the headless path, a raised SDK exception for the eval path) before
    handing the text here — this function only classifies the text.
    """
    haystack = text.casefold()
    for phrase, cause in _SIGNATURES:
        if phrase in haystack:
            return LimitMatch(phrase=phrase, cause=cause)
    return None


class CreditExhaustedError(RuntimeError):
    """The billed ``ANTHROPIC_API_KEY`` has a $0 balance (an :data:`LimitCause.API_CREDIT`).

    A real ``/v1/messages`` call on a credit-empty key returns HTTP 400 (credit
    balance too low). That is NOT a per-scenario cap and NOT a generic crash —
    nothing more can execute until credits are added — so the metered-eval lane
    raises this distinct, actionable error (carrying the console remediation)
    rather than redding every remaining scenario behind an opaque error result.
    """

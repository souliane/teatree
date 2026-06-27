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

Two inputs classify a signal, preferred in this order:

- The SDK's TYPED window — ``claude_agent_sdk.types.RateLimitInfo.rate_limit_type``
    (``five_hour`` / ``seven_day`` / ``seven_day_opus`` / ``seven_day_sonnet`` /
    ``overage``). This is unambiguous structured data, so when it is available
    (a ``RateLimitEvent`` in the message stream) :func:`classify_rate_limit_type`
    maps it directly — no prose-grep, no chance of mislabeling a 7-day window as a
    5-hour one.
- The raw error TEXT — phrase-matched by :func:`classify_limit` as the fallback
    for an error string that carries no typed field (e.g. an HTTP 400 ``result``
    string surfaced by the SDK).

Phrase provenance (finding-4 honesty — the phrases are NOT all verbatim static
strings, so this does not claim they are). Most are grepped from the bundled
``claude`` CLI binary, with occurrence counts where confirmed: ``out_of_credits``
(x16, the structured error code), ``out of usage credits`` (x12), ``credit
balance (?:is )?too low`` (x7), ``weekly limit`` (x13), ``7-day limit`` (x1),
``Opus limit`` / ``Sonnet limit`` (x8 each, the per-model 7-day caps), ``session
limit`` (x12), ``usage limit`` (x31), ``rate limit`` (x81). The remaining entries
are DEFENSIVE human-readable renderings of the same windows that the CLI composes
dynamically (e.g. the statusline labels "5-hour and 7-day limits"): ``out of
credits``, ``seven-day limit``, ``5-hour limit``, ``five-hour limit``. One entry
is mapped on API semantics rather than a literal match: ``quota exceeded`` — in
the binary the literal string is only the libc "Disk quota exceeded" error, so it
is mapped to the transient :data:`LimitCause.RATE_LIMIT` (the API's rate-quota
wording, keeping pre-PR parity), never to a subscription or credit cause. A
credit-empty condition is never laundered into a subscription-quota report.
"""

import dataclasses
from enum import Enum

from claude_agent_sdk.types import RateLimitType


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
#: subscription phrase (their remediation is unrelated to any plan); the weekly
#: phrases (incl. the per-model Opus/Sonnet 7-day caps) precede the generic
#: session ``usage limit`` so a 7-day message is never mislabeled a 5-hour one;
#: ``rate limit`` / ``quota exceeded`` are last (the lowest-priority transient
#: bucket). See the module docstring for each phrase's provenance and counts.
_SIGNATURES: tuple[tuple[str, LimitCause], ...] = (
    # API-key CREDIT / metered usage-based-billing exhaustion (a $0 balance).
    ("credit balance too low", LimitCause.API_CREDIT),  # CLI x3
    ("credit balance is too low", LimitCause.API_CREDIT),  # CLI x4
    ("out of usage credits", LimitCause.API_CREDIT),  # CLI x12
    ("out_of_credits", LimitCause.API_CREDIT),  # CLI x16 (the structured error code)
    ("out of credits", LimitCause.API_CREDIT),  # human-readable rendering
    # Subscription WEEKLY (7-day) windows — the plan-wide cap and the per-model
    # Opus/Sonnet 7-day caps (the SDK's seven_day_opus / seven_day_sonnet).
    ("weekly limit", LimitCause.SUBSCRIPTION_WEEKLY),  # CLI x13
    ("7-day limit", LimitCause.SUBSCRIPTION_WEEKLY),  # CLI x1 (the statusline window label)
    ("seven-day limit", LimitCause.SUBSCRIPTION_WEEKLY),  # human-readable rendering
    ("opus limit", LimitCause.SUBSCRIPTION_WEEKLY),  # CLI x8 (the seven_day_opus prose label)
    ("sonnet limit", LimitCause.SUBSCRIPTION_WEEKLY),  # CLI x8 (the seven_day_sonnet prose label)
    # Subscription SESSION (~5h rolling) window.
    ("5-hour limit", LimitCause.SUBSCRIPTION_SESSION),  # the five_hour window rendered in prose
    ("five-hour limit", LimitCause.SUBSCRIPTION_SESSION),  # human-readable rendering
    ("session limit", LimitCause.SUBSCRIPTION_SESSION),  # CLI x12
    ("usage limit", LimitCause.SUBSCRIPTION_SESSION),  # CLI x31
    # Transient API rate / quota limit (HTTP 429).
    ("rate limit", LimitCause.RATE_LIMIT),  # CLI x81
    ("quota exceeded", LimitCause.RATE_LIMIT),  # API rate-quota wording (see docstring)
)

#: The SDK's TYPED rate-limit window -> cause. This is the unambiguous,
#: structured-data path (``RateLimitInfo.rate_limit_type``), preferred over
#: prose-grep whenever a ``RateLimitEvent`` is available: the five-hour window is
#: the rolling SESSION limit, every seven-day window (plan-wide + the per-model
#: Opus/Sonnet caps) is the WEEKLY limit, and an ``overage`` window is the
#: usage-based-billing credit cause (its remediation is to add credits, never to
#: wait out a plan reset).
_RATE_LIMIT_TYPE_CAUSES: dict[RateLimitType, LimitCause] = {
    "five_hour": LimitCause.SUBSCRIPTION_SESSION,
    "seven_day": LimitCause.SUBSCRIPTION_WEEKLY,
    "seven_day_opus": LimitCause.SUBSCRIPTION_WEEKLY,
    "seven_day_sonnet": LimitCause.SUBSCRIPTION_WEEKLY,
    "overage": LimitCause.API_CREDIT,
}

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


def classify_rate_limit_type(rate_limit_type: RateLimitType | None) -> LimitMatch | None:
    """Classify the SDK's TYPED :attr:`RateLimitInfo.rate_limit_type` window into its cause.

    This is the unambiguous, structured-data path — preferred over
    :func:`classify_limit`'s prose-grep whenever a ``RateLimitEvent`` is available
    (it cannot mislabel a 7-day window as a 5-hour one). ``None`` (no typed field)
    or an unrecognized window value falls through to ``None`` so the caller can
    drop back to phrase-matching the raw error text. The match's ``phrase`` is the
    window token itself, so :meth:`LimitMatch.as_reason` records ``seven_day_opus``
    et al. verbatim.
    """
    if rate_limit_type is None:
        return None
    cause = _RATE_LIMIT_TYPE_CAUSES.get(rate_limit_type)
    return LimitMatch(phrase=rate_limit_type, cause=cause) if cause is not None else None


class CreditExhaustedError(RuntimeError):
    """The billed ``ANTHROPIC_API_KEY`` has a $0 balance (an :data:`LimitCause.API_CREDIT`).

    A real ``/v1/messages`` call on a credit-empty key returns HTTP 400 (credit
    balance too low). That is NOT a per-scenario cap and NOT a generic crash —
    nothing more can execute until credits are added — so the metered-eval lane
    raises this distinct, actionable error (carrying the console remediation)
    rather than redding every remaining scenario behind an opaque error result.
    """

"""Classification of the Agent SDK's error-result terminus for the eval runner.

The SDK exposes no typed error-result exception: a capped or mislabeled run
surfaces as a bare ``Exception`` whose message carries the CLI's own error
string. This module turns that string into a graded outcome — a terminal cap
reason (budget / max-turns), a mislabeled-success signal, or the partial cost —
and defines the trajectory-carrying exceptions (:class:`TerminalResultError`,
:class:`SuccessMislabelResultError`) the runner raises mid-stream and grades.
"""

import dataclasses
import re
from enum import Enum

from claude_agent_sdk import Message

from teatree.llm.anthropic_limits import LimitCause, classify_limit, window_horizon

BUDGET_EXCEEDED_REASON = "budget_exceeded"
MAX_TURNS_REASON = "max_turns"

#: The ``terminal_reason`` prefix a throttle that outlasted its bounded retry
#: budget carries (built in :func:`teatree.eval.throttle_retry.throttle_reason`).
#: The single source both that surface and the CI-eval triage classifier
#: (:func:`teatree.eval.triage.classify_red`) key on, so a "throttled:" red is
#: never matched by a divergent hardcoded copy.
THROTTLE_TERMINAL_PREFIX = "throttled:"

#: The SDK has no typed error-result exception: when a run hits a cap the CLI
#: emits an ``is_error`` ``result`` event and exits non-zero, which the SDK's
#: ``receive_messages`` (``claude_agent_sdk/_internal/query.py`` L852) surfaces as
#: a bare ``Exception`` whose message is ``"Claude Code returned an error result:
#: <subtype-or-errors>"`` (built at L342). The trailing text is the CLI's own
#: error string, so each terminal subtype is identified by a stable substring:
#:
#: * ``error_max_budget_usd`` -> ``"Reached maximum budget ($0.1)"``
#: * ``error_max_turns``      -> ``"Reached maximum number of turns (3)"``
#:
#: A capped run is a GRADED terminus (the agent ran out of room), not an infra
#: failure — so each marker maps to a ``terminal_reason``. Anything NOT matched
#: here is a genuine error and re-raises, so a real crash is never swallowed as a
#: graded cell. Extend by adding a ``(marker, reason)`` pair.
_TERMINAL_MARKERS: tuple[tuple[str, str], ...] = (
    ("maximum budget", BUDGET_EXCEEDED_REASON),
    ("maximum number of turns", MAX_TURNS_REASON),
)
#: The SDK wraps the CLI's non-zero exit as ``"Claude Code returned an error
#: result: <subtype-or-errors>"`` (``claude_agent_sdk/_internal/query.py`` L342)
#: whenever a ``result`` event carried ``is_error=True`` — but the descriptive
#: field it falls back to is the ``subtype``, which the CLI sometimes reports as
#: ``"success"`` even while exiting non-zero. That is a SUCCESSFUL terminus
#: mislabeled, NOT a cap and NOT a crash: the captured trajectory already holds
#: the success ``result`` event, so the run is graded normally instead of raised.
_SUCCESS_RESULT_MARKER = "returned an error result: success"
#: The cap the SDK reports in the budget message — ``Reached maximum budget
#: ($0.1)`` — is the partial-cost floor when no metered ``result`` event was
#: produced. (max-turns carries no ``($X)``; its cost comes from a captured
#: ``ResultMessage`` if any, else ``0.0``.)
_BUDGET_AMOUNT_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)")


def classify_terminal_error(message: str) -> str | None:
    """Map an SDK error-result *message* to a graded ``terminal_reason``, or ``None``.

    Returns the ``terminal_reason`` for a known terminal cap (budget, max-turns —
    see :data:`_TERMINAL_MARKERS`) when the message carries that cap's marker
    substring, else ``None`` for a genuine error the runner must re-raise. The
    markers are the CLI's own error-result strings (see :data:`_TERMINAL_MARKERS`
    for provenance); the list is the single place to extend with a new cap.
    """
    for marker, reason in _TERMINAL_MARKERS:
        if marker in message:
            return reason
    return None


def is_success_result_error(message: str) -> bool:
    """``True`` when the SDK's error-result *message* actually describes a SUCCESS.

    The CLI can exit non-zero while its ``result`` event subtype reads
    ``"success"``; the SDK then raises ``"...returned an error result: success"``.
    Treating that as a genuine error would crash a finished run, so the runner
    recognizes it and grades the captured trajectory normally (the success
    ``result`` event is already in the captured messages).
    """
    return _SUCCESS_RESULT_MARKER in message


class ThrottleKind(Enum):
    """The retry disposition a transient throttle demands.

    ``TRANSIENT`` clears within minutes (a 429, an overloaded 529, a dropped
    stream, a generic under-load exit-1) and is ridden out with fast exponential
    backoff. ``SUSTAINED`` is a rolling subscription-window cap that needs a
    (bounded) window wait before the token frees up.
    """

    TRANSIENT = "transient"
    SUSTAINED = "sustained"


@dataclasses.dataclass(frozen=True)
class ThrottleSignal:
    """A throttle graded from a raw SDK error message: its kind, cause, and wait.

    ``cause`` is the classified :class:`~teatree.llm.anthropic_limits.LimitCause`
    when a limit phrase matched, else ``None`` for a generic transient drop.
    ``wait_seconds`` carries the SUSTAINED window horizon (the caller clamps it to
    a bounded cap); it is ``None`` for a TRANSIENT signal, which uses the caller's
    exponential backoff instead.
    """

    kind: ThrottleKind
    cause: LimitCause | None
    wait_seconds: float | None


#: Substrings marking a TRANSIENT throttle/transport signal the phrase taxonomy
#: does not carry. Kept DELIBERATELY specific: an OPAQUE SDK error result with no
#: recognizable throttle signature is NOT laundered into a retry — it re-raises as
#: a genuine crash (the "preserve the genuine-crash red" contract). ``overloaded``
#: is HTTP 529; ``too many requests`` / ``rate_limit_error`` are the 429 status
#: text and API error type; the rest are dropped-stream signatures under load.
_TRANSIENT_INFRA_MARKERS: tuple[str, ...] = (
    "overloaded",
    "too many requests",
    "rate_limit_error",
    "connection error",
    "connection reset",
    "connection aborted",
    "server disconnected",
    "peer closed connection",
    "incomplete read",
    "stream error",
    "read timed out",
)

#: Substrings marking a mid-stream SDK TRANSPORT CRASH — the subprocess CLI dying
#: with NO ``result`` event, surfaced by the message reader's bare ``ProcessError``
#: (``subprocess_cli.py`` L711) via its ``"Fatal error in message reader"`` branch
#: (``_internal/query.py`` L351). This is the anti-cheat boundary's SAFE side: the
#: crash aborted the run before any trajectory was captured (0-byte, no verdict),
#: so re-running the scenario launders nothing. It is DISTINCT from a behavioral
#: cap by SDK construction — when the CLI DID emit a ``result`` event (a genuine
#: max-turns/budget cap, or an ``error_during_execution`` terminus), the message
#: reader REPLACES this ProcessError with ``"Claude Code returned an error result:
#: …"`` (``query.py`` L342), which carries a real trajectory and is NEVER matched
#: here. So this marker fires only when no verdict exists — retry-safe.
_TRANSPORT_CRASH_MARKERS: tuple[str, ...] = ("command failed with exit code",)

#: Substrings marking a host-resource-starvation transient during eval PROVISIONING
#: — the per-run ephemeral checkout's ``git clone`` (or repo-root resolution) failing
#: under a momentary RAM spike, raised as ``EphemeralCheckoutError`` (both raise sites,
#: ``ephemeral_checkout.py`` L115/L126, begin with this substring). Same SAFE side of
#: the anti-cheat boundary as a transport crash: the scenario aborted during setup, so
#: NO trajectory and NO behavioral verdict were produced — a retry after the spike
#: clears re-runs a cell that graded nothing, laundering nothing. A provisioning
#: failure is never a ``result`` event, so it can never collide with a behavioral cap.
_PROVISION_TRANSIENT_MARKERS: tuple[str, ...] = ("cannot provision an isolated ephemeral checkout",)

#: Limit causes that are NEVER a retriable throttle: a $0 metered key has no
#: time-based recovery (fail loud), and a 7-day weekly cap is never worth waiting
#: out inside a single run (surface loud). The remaining causes ARE retriable —
#: RATE_LIMIT as TRANSIENT, SUBSCRIPTION_SESSION as a bounded SUSTAINED window wait.
_NEVER_RETRY_CAUSES: frozenset[LimitCause] = frozenset({LimitCause.API_CREDIT, LimitCause.SUBSCRIPTION_WEEKLY})


def _throttle_from_limit(cause: LimitCause) -> ThrottleSignal | None:
    """Map a matched limit *cause* to its retry disposition, or ``None`` when never-retriable."""
    if cause in _NEVER_RETRY_CAUSES:
        return None
    if cause is LimitCause.SUBSCRIPTION_SESSION:
        horizon = window_horizon(cause)
        wait = horizon.total_seconds() if horizon is not None else None
        return ThrottleSignal(kind=ThrottleKind.SUSTAINED, cause=cause, wait_seconds=wait)
    return ThrottleSignal(kind=ThrottleKind.TRANSIENT, cause=cause, wait_seconds=None)


def classify_transient_throttle(message: str) -> ThrottleSignal | None:
    """Grade an SDK error *message* into a retry disposition, or ``None`` if not a throttle.

    Returns a :class:`ThrottleSignal` for a retriable throttle and ``None`` for
    everything the runner must NOT ride out: a genuine behavioral cap
    (budget/max-turns — the anti-cheat boundary), a credit exhaustion or weekly
    cap (surface loud), a mislabeled success, or an opaque behavioral error the
    CLI reported via a ``result`` event. A matched limit phrase drives the
    disposition (RATE_LIMIT -> TRANSIENT, SUBSCRIPTION_SESSION -> SUSTAINED);
    otherwise a transient infra marker (:data:`_TRANSIENT_INFRA_MARKERS`), a
    mid-stream transport crash (:data:`_TRANSPORT_CRASH_MARKERS`), or a
    provisioning transient (:data:`_PROVISION_TRANSIENT_MARKERS`) — each a
    subprocess/setup death with NO captured trajectory — grades as TRANSIENT.
    """
    if is_success_result_error(message) or classify_terminal_error(message) is not None:
        return None
    limit = classify_limit(message)
    if limit is not None:
        return _throttle_from_limit(limit.cause)
    haystack = message.casefold()
    transient_markers = (*_TRANSIENT_INFRA_MARKERS, *_TRANSPORT_CRASH_MARKERS, *_PROVISION_TRANSIENT_MARKERS)
    if any(marker in haystack for marker in transient_markers):
        return ThrottleSignal(kind=ThrottleKind.TRANSIENT, cause=None, wait_seconds=None)
    return None


def budget_amount_from_message(message: str) -> float | None:
    """Return the ``$X`` amount the SDK message names, or ``None`` when absent.

    The budget cap message carries the spend at truncation (``Reached maximum
    budget ($0.1)``); the max-turns message carries none. ``None`` lets the caller
    pick a per-terminus fallback (the cap for budget, a captured ``ResultMessage``
    cost or ``0.0`` for max-turns).
    """
    match = _BUDGET_AMOUNT_RE.search(message)
    return float(match.group(1)) if match else None


def budget_floor_from_message(message: str, *, cap: float) -> float:
    """Recover the partial cost from the SDK's ``Reached maximum budget ($X)``.

    Returns the amount the message names (the spend at truncation) when present,
    else the configured *cap* as a floor — an over-budget cell always reports a
    real cost, never a misleading ``0.0``/blank.
    """
    amount = budget_amount_from_message(message)
    return amount if amount is not None else cap


class TerminalResultError(Exception):
    """A known terminal cap (budget/max-turns) the SDK surfaced mid-stream.

    Carries the partial trajectory ``_collect`` gathered before the cap plus the
    classified ``terminal_reason``, so the runner can grade the REAL trajectory
    instead of discarding every message the all-or-nothing comprehension held.
    """

    def __init__(self, *, terminal_reason: str, messages: list[Message], cause: Exception) -> None:
        super().__init__(str(cause))
        self.terminal_reason = terminal_reason
        self.messages = messages
        self.cause = cause


class SuccessMislabelResultError(Exception):
    """A finished SUCCESS the CLI mislabeled by exiting non-zero on a ``"success"`` subtype.

    The captured ``result`` event already reads ``subtype="success"`` but also
    carries a stray ``is_error=True`` (the CLI exited non-zero), so grading the
    trajectory as-is would force a finished, all-matchers-pass run to FAIL on the
    flag alone. Carries the captured trajectory so the runner can grade the REAL
    messages and clear ``is_error`` — the same correction
    :meth:`ApiInProcessRunner._terminal_capped_run` applies to a capped run.
    """

    def __init__(self, *, messages: list[Message], cause: Exception) -> None:
        super().__init__(str(cause))
        self.messages = messages
        self.cause = cause

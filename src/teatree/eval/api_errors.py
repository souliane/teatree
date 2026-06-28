"""Classification of the Agent SDK's error-result terminus for the eval runner.

The SDK exposes no typed error-result exception: a capped or mislabeled run
surfaces as a bare ``Exception`` whose message carries the CLI's own error
string. This module turns that string into a graded outcome — a terminal cap
reason (budget / max-turns), a mislabeled-success signal, or the partial cost —
and defines the trajectory-carrying exceptions (:class:`TerminalResultError`,
:class:`SuccessMislabelResultError`) the runner raises mid-stream and grades.
"""

import re

from claude_agent_sdk import Message

BUDGET_EXCEEDED_REASON = "budget_exceeded"
MAX_TURNS_REASON = "max_turns"

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

"""Pure repair-loop budget + stall policy (#2009).

The leaf policy half of two repair-loop robustness gaps; the model-touching
orchestration that applies it lives on the ``TaskAttempt`` / ``Task`` models
(``models/task.py``), keeping this module a dependency-free leaf both
``teatree.core.models`` and ``teatree.core.managers`` may depend on.

Visible iteration budget — :func:`max_phase_iterations` is the configurable
per-phase cap; :func:`requeue_verdict` raises :class:`MaxIterationsExceeded`
when a ticket-phase has spent it, so a phase cannot retry forever on the
time-based stale-task expiry alone.

Stall detection — :func:`terminal_reason_fingerprint` is a stable hash of a
terminal reason, normalized so transient noise (timestamps, pids, hex ids,
paths, bare numbers) does not defeat the "identical failure" check. Two
consecutive identical fingerprints is a stall: :func:`requeue_verdict` raises
:class:`IterationStalled` so the caller escalates to the user instead of
re-running the identical failure.
"""

import hashlib
import re

from django.conf import settings

DEFAULT_MAX_PHASE_ITERATIONS = 5

# Two consecutive identical fingerprints on the same phase ⇒ stall.
STALL_REPEAT_THRESHOLD = 2

_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}[ t]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"),  # ISO timestamp
    re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"),  # bare clock time
    re.compile(r"\bpid[=:\s]+\d+\b", re.IGNORECASE),  # pid=12345
    re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE),  # 0xdeadbeef
    re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE),  # long hex / uuid-ish ids
    re.compile(r"(?:/[\w.+-]+){2,}"),  # volatile absolute fs paths (tmp dirs, worktrees)
    re.compile(r"\b\d+\b"),  # residual bare numbers (line/port/counts)
)


class RepairLoopError(RuntimeError):
    """Base for repair-loop budget / stall refusals."""

    def __init__(self, message: str, *, ticket_id: int, phase: str) -> None:
        super().__init__(message)
        self.ticket_id = ticket_id
        self.phase = phase


# The ticket-mandated public names of the two terminal repair-loop refusals
# (souliane/teatree#2009); they are the contract callers catch, so they keep
# the domain names rather than an ``…Error`` suffix.
class MaxIterationsExceeded(RepairLoopError):  # noqa: N818 — ticket-mandated name (#2009)
    """A ticket-phase reached its configured iteration cap and may not re-queue."""


class IterationStalled(RepairLoopError):  # noqa: N818 — ticket-mandated name (#2009)
    """A ticket-phase failed identically twice in a row — escalate, do not re-queue."""


def max_phase_iterations() -> int:
    """Configured per-phase iteration cap (``MAX_PHASE_ITERATIONS``, floor 1).

    A non-positive or absent setting degrades to :data:`DEFAULT_MAX_PHASE_ITERATIONS`
    so a fat-fingered ``0`` can never trap a phase the instant it starts.
    """
    raw = getattr(settings, "MAX_PHASE_ITERATIONS", DEFAULT_MAX_PHASE_ITERATIONS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PHASE_ITERATIONS
    return max(1, value)


def normalize_terminal_reason(reason: str) -> str:
    """Collapse a terminal reason to a stable canonical string.

    Masks transient noise (timestamps, pids, hex ids, paths, bare numbers) and
    folds case + whitespace, so two runs of the *same* failure normalize
    identically while two genuinely different failures stay distinct.
    """
    text = reason.strip().lower()
    if not text:
        return ""
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub("\x00", text)
    return " ".join(text.split())


def terminal_reason_fingerprint(reason: str) -> str:
    """Stable hex hash of the normalized terminal reason; ``""`` for an empty reason."""
    normalized = normalize_terminal_reason(reason)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_stalled(last_two_fingerprints: list[str]) -> bool:
    """True iff the last two non-empty fingerprints are identical (a stall)."""
    return len(last_two_fingerprints) == STALL_REPEAT_THRESHOLD and len(set(last_two_fingerprints)) == 1


def requeue_verdict(
    *,
    ticket_id: int,
    phase: str,
    iteration_count: int,
    last_two_fingerprints: list[str],
) -> None:
    """Raise if a ticket-phase may NOT be re-queued; a no-op when it may.

    The pure decision over primitives the model layer supplies. The stall check
    is evaluated FIRST so an identical double-failure escalates even before the
    raw cap is reached:

    * **Stall** — two consecutive identical fingerprints → :class:`IterationStalled`.
    * **Cap** — at or over :func:`max_phase_iterations` → :class:`MaxIterationsExceeded`.
    """
    if is_stalled(last_two_fingerprints):
        msg = (
            f"phase {phase!r} on ticket {ticket_id} stalled: two consecutive "
            f"identical failures (fingerprint {last_two_fingerprints[0][:12]})."
        )
        raise IterationStalled(msg, ticket_id=ticket_id, phase=phase)
    cap = max_phase_iterations()
    if iteration_count >= cap:
        msg = f"phase {phase!r} on ticket {ticket_id} hit the iteration cap ({iteration_count}/{cap})."
        raise MaxIterationsExceeded(msg, ticket_id=ticket_id, phase=phase)

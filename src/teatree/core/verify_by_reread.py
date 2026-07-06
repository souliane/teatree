"""Verify-by-reread: the small core contract for post-write side-effect verification (#1192).

A successful-looking write to an external system — Slack's ``reactions.add``
returning ``ok: true``, a harness ``CronCreate`` call returning without error —
is not proof the effect is actually visible. Eventual consistency, a stale
read replica, or a race between the write and the next read can all make a
claimed write unobservable moments later. "The write call said it worked" and
"the effect is actually there" are different claims; only re-reading the
target system independently closes the gap between them.

This module is that contract, factored out once so every call site applies
the same shape rather than hand-rolling its own try/except: call ``reread``,
normalize whatever it returns (or raises) into a :class:`RereadOutcome`, and
never let a verification failure raise into the caller — the write already
happened by the time verification runs, so a failed verification is a signal
to log, retry, or flag, never a reason to crash the write path.

Call sites wire this in (#1192, subsuming #1193 and #1202; #1194):

*   :func:`teatree.backends.slack.reactions.add_reaction_verified` re-reads a
    posted Slack reaction via ``reactions.get`` before trusting ``reactions.add``.
*   :func:`teatree.core.merge.pr_create_verify.verify_pr_exists` re-reads a just-created
    PR's open-state before the ship/ensure path trusts ``create_pr`` and records
    the URL, so a phantom PR (a 404 re-read) never advances the FSM (#1194).

See BLUEPRINT.md §17.1 invariant 13 for the architectural statement.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RereadOutcome:
    """The verdict of one independent re-read of a claimed external write."""

    confirmed: bool
    reason: str = ""

    @classmethod
    def confirmed_ok(cls) -> "RereadOutcome":
        return cls(confirmed=True)

    @classmethod
    def not_confirmed(cls, reason: str) -> "RereadOutcome":
        return cls(confirmed=False, reason=reason)


def verify_by_reread(*, label: str, reread: Callable[[], bool]) -> RereadOutcome:
    """Re-read a claimed write and normalize the result to a :class:`RereadOutcome`.

    ``reread`` performs a fresh, independent observation of the target system
    — never the write call's own response — and returns ``True`` when the
    effect is visible, ``False`` when the reread succeeded but did not observe
    it. Any exception ``reread`` raises (a transport failure, an API error) is
    caught here and reported as ``not_confirmed`` rather than propagated: the
    write has already happened, so a broken reread must degrade to "could not
    confirm", never crash the caller.

    ``label`` identifies the write being verified in the log line and the
    outcome's ``reason`` on the exception path, so a caller juggling several
    concurrent verifications can tell them apart in its logs.
    """
    try:
        observed = reread()
    except Exception as exc:  # noqa: BLE001 — a broken reread must degrade, never crash the write path.
        logger.warning("verify_by_reread(%s): reread raised: %s", label, exc)
        return RereadOutcome.not_confirmed(f"reread raised: {exc}")
    if observed:
        return RereadOutcome.confirmed_ok()
    return RereadOutcome.not_confirmed(f"{label}: reread did not observe the write")


__all__ = ["RereadOutcome", "verify_by_reread"]

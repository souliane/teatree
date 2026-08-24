"""The one t3-master gate the reactive-loop cycles share (#3968).

``loop_slack_answer`` and ``loop_self_improve`` each carried their own copy of a
``_session_owns_loop`` predicate that read ``loop-registry.json``'s
``t3-loop-tick-owner`` record. Nothing prunes that file, so a stale id left by a
dead session locked both loops out permanently — while ``t3 loop owner``, which
reads the DB ``t3-master`` :class:`~teatree.core.models.LoopLease`, reported the
slot UNCLAIMED. Two representations of one fact, free to diverge, with the gate
enforcing the one an operator never looks at.

This module makes the DB lease the single authority, so the gate and
``t3 loop owner`` can only ever agree, and splits the old single "not the owner"
verdict into the two conditions that call for opposite operator responses:

- :attr:`T3MasterGate.UNCLAIMED` — the lease is unheld;
- :attr:`T3MasterGate.FOREIGN_OWNER` — a different live session holds it (leave it alone).

Each verdict names ONLY the lease, and asserts nothing about whether the factory is
running (#4253). An unheld lease and an idle factory are different facts, and this gate
establishes just the first: on the box that produced the ticket every loop was ticking
and the worker flock was held while the slot read unheld, so a verdict phrased as a
claim about the whole system was false, and its "start a worker" advice would have run a
second worker against one control DB. The contradiction between the two facts belongs to
``t3 doctor check``, which reads both.

Exactly two callers consult this gate, and both are reactive ``/loop`` cycles that
would otherwise run against a factory nobody owns:
:mod:`teatree.core.management.commands.loop_slack_answer` and
:mod:`teatree.core.management.commands.loop_self_improve`. The set is pinned by
``tests/conformance/test_t3_master_gate_consumers.py`` so a third gated cycle is a
deliberate, visible addition rather than another loop that silently no-ops.

A slot owned by :data:`~teatree.core.session_identity.LOOP_RUNNER_SESSION_ID` runs
for ANY caller: the loop runner is the machine-wide driver rather than a competing
session, and the per-cycle ``loop-<name>`` lease each command takes is what actually
serialises the work — the same reasoning ``loop_drain_queue`` documents for carrying
no owner gate at all.
"""

from dataclasses import dataclass
from enum import StrEnum

from teatree.core.loop_lease_manager import T3_MASTER_SLOT
from teatree.core.models import LoopLease
from teatree.core.session_identity import is_loop_runner_session, loop_principal


class T3MasterGate(StrEnum):
    """Whether a reactive-loop cycle may run, and if not, WHICH condition stopped it."""

    RUN = "run"
    UNCLAIMED = "t3_master_unclaimed"
    FOREIGN_OWNER = "t3_master_foreign_owner"


_UNCLAIMED_MESSAGE = (
    "SKIP  the `t3-master` owner lease is unheld — skipping {cycle} cycle. That lease is the only "
    "thing read here; whether loops are ticking is a separate fact this says nothing about. "
    "`t3 doctor check` reads both and reports the contradiction."
)
_FOREIGN_MESSAGE = "SKIP  t3-master is owned by another live session ({owner}) — skipping {cycle} cycle."


@dataclass(frozen=True, slots=True)
class T3MasterVerdict:
    """The gate's answer: the outcome plus the owning session it was read from."""

    outcome: T3MasterGate
    owner_session: str

    @property
    def may_run(self) -> bool:
        return self.outcome is T3MasterGate.RUN

    def skip_message(self, cycle: str) -> str:
        """The human SKIP line naming which condition fired; ``""`` when the cycle may run."""
        if self.outcome is T3MasterGate.UNCLAIMED:
            return _UNCLAIMED_MESSAGE.format(cycle=cycle)
        if self.outcome is T3MasterGate.FOREIGN_OWNER:
            return _FOREIGN_MESSAGE.format(owner=self.owner_session, cycle=cycle)
        return ""


def t3_master_verdict(caller_session: str | None = None) -> T3MasterVerdict:
    """Resolve the gate for *caller_session* (default: this process's loop principal).

    ``caller_session`` is resolved through :func:`loop_principal` — the one identity
    seam ``t3 loop claim``/``release`` and the per-loop tick already share — so the
    principal a claim binds is exactly the one this gate matches.
    """
    status = LoopLease.objects.ownership_status(T3_MASTER_SLOT)
    if not status.is_live:
        return T3MasterVerdict(outcome=T3MasterGate.UNCLAIMED, owner_session="")
    session = loop_principal()[0] if caller_session is None else caller_session
    if is_loop_runner_session(status.owner_session) or status.owner_session == session:
        return T3MasterVerdict(outcome=T3MasterGate.RUN, owner_session=status.owner_session)
    return T3MasterVerdict(outcome=T3MasterGate.FOREIGN_OWNER, owner_session=status.owner_session)


def live_foreign_owner_session(session_id: str, *, current_pid: int | None) -> str:
    """The live FOREIGN session holding ``t3-master`` for *session_id*, ``""`` when none.

    The SessionStart tick-owner election's read. The ``t3 worker`` is exempt: it is
    the machine-wide driver, and it holds the slot for as long as it drives ticks —
    reported as a rival, it would leave the tick-owner record permanently unclaimed,
    so no session would register the three reactive ``/loop`` slots and #3968 would
    re-break one layer up. Only the DB CAS treats the runner as an ordinary owner,
    so a live lease is still never evicted.
    """
    owner = LoopLease.objects.live_foreign_owner(T3_MASTER_SLOT, session_id=session_id, current_pid=current_pid)
    return "" if is_loop_runner_session(owner) else owner


__all__ = ["T3MasterGate", "T3MasterVerdict", "live_foreign_owner_session", "t3_master_verdict"]

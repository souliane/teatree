"""Loop-owner collision guardrails for the inert maker-only pane layer (#1838 PR#7a).

Safety-critical, and inert: nothing in the live loop / dispatch / claim path
imports this module while the pane layer ships dark (the #2320 AST inertness
scan pins that). A LATER PR (#7b) calls these guards at the pane spawn / claim
boundary.

Two guards, both leaning on the PROVEN disjoint key spaces
(:mod:`teatree.core.loop_lease_manager`).

:func:`assert_pane_claim_allowed` is a hard boundary guard that a team-role pane
claims ONLY its ``team:<role>`` slot and can NEVER claim the global ``loop-owner``
slot (:data:`GLOBAL_OWNER_SLOT`) or a ``loop:<name>`` per-loop slot
(:data:`PER_LOOP_OWNER_PREFIX`). It FAILS CLOSED — anything not provably a
``team:<role>`` slot is rejected, so a pane can never claim an infra lease
(``loop-tick`` / …) either.

:func:`live_owner_blocks_pane` is the pre-work live-owner check: a pane SKIPS
(the #744 zeroed-contract path) when a LIVE ``loop-owner`` exists and it is a
different session, so a pane burns no resources during another session's live
loop. It reuses the pid-anchored owner-liveness read
(:meth:`LoopLease.ownership_status`) so it can never drift from the loop-owner
doctrine.
"""

from teatree.core.loop_lease_manager import GLOBAL_OWNER_SLOT
from teatree.teams.roles import is_team_claim_slot


class LoopOwnerCollisionError(RuntimeError):
    """A team-role pane attempted to claim a non-team slot (loop-owner / infra)."""


def assert_pane_claim_allowed(slot: str) -> None:
    """Raise unless *slot* is a ``team:<role>`` claim key (#1838 PR#7a).

    The single claim-boundary guard: a pane may claim ONLY its canonical
    ``team:<role>`` slot. The check is the positive predicate
    :func:`teatree.teams.roles.is_team_claim_slot` (``team:`` prefix), so it
    fails CLOSED — the global ``loop-owner`` slot, any ``loop:<name>`` per-loop
    slot, an infra lease (``loop-tick`` / …), and the empty string are all
    rejected. Relying on the positive ``team:`` predicate (not a denylist of
    forbidden slots) means a future owner-namespace slot is rejected by default.
    """
    if not is_team_claim_slot(slot):
        msg = (
            f"A team-role pane may claim only a 'team:<role>' slot, never {slot!r}. "
            f"The loop-owner slot ({GLOBAL_OWNER_SLOT!r}) and the 'loop:' per-loop "
            "namespace are reserved for loop ownership and are disjoint from 'team:'."
        )
        raise LoopOwnerCollisionError(msg)


def live_owner_blocks_pane(*, pane_session_id: str) -> bool:
    """True iff a LIVE foreign ``loop-owner`` should make this pane skip (#744 / #1838).

    The pre-work live-owner check. Returns ``True`` when a non-empty
    ``loop-owner`` claim is live (pid-anchored — unexpired TTL OR an alive
    ``owner_pid``) AND its session is NOT *pane_session_id*: the pane is not the
    loop owner and a live loop is running, so it skips via the zeroed-contract
    path rather than burning resources. Returns ``False`` when the slot is
    unowned, owned by this pane's own session, or held by a dead/expired
    (reclaimable) owner. Reuses :meth:`LoopLease.ownership_status` so the
    liveness predicate is the same one ``claim_ownership`` / ``evict_stale_owner``
    use — it can never drift from the loop-owner doctrine.
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415

    status = LoopLease.objects.ownership_status(GLOBAL_OWNER_SLOT)
    if not status.is_live:
        return False
    return status.owner_session != pane_session_id


__all__ = ["LoopOwnerCollisionError", "assert_pane_claim_allowed", "live_owner_blocks_pane"]

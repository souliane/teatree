"""Agent-teams WORK layer — the inert team-role registry (#1838 Track-B PR#6).

The WORK-team is the optional, pane-backed work-execution + context-
specialisation layer that sits ON TOP of the loops (which stay on the lead
session — no loop ever lives on a teammate). It declares three roles —
CORE-MAKER, OVERLAY-MAKER, REVIEWER — each with a canonical ``claimed_by``
key in the ``team:<role>`` namespace (disjoint from the loop-owner / per-loop /
infra slots), and each maker role a declarative overlay-seam claim filter that
partitions the backlog (CORE → ``overlay == ""``, OVERLAY → ``overlay != ""``).

This PR ships the registry DARK behind the default-OFF ``[teams] enabled``
toggle: the module is PURE DATA, imports nothing from ``teatree`` (a foundation
leaf), and is referenced by NOTHING in the loop / dispatch / claim execution
path. The pane-spawn helper and the live REVIEWER/maker teammates are deferred
to later PRs. Nothing here runs when the flag is off; nothing runs when it is
on either, until a future PR wires a consumer in.
"""

from teatree.teams.roles import TEAM_CLAIM_PREFIX, TeamRole, is_team_claim_slot, team_claim_slot

__all__ = [
    "TEAM_CLAIM_PREFIX",
    "TeamRole",
    "is_team_claim_slot",
    "team_claim_slot",
]

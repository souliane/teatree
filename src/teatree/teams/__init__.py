"""Agent-teams WORK layer — the inert team-role registry (#1838 Track-B PR#6).

The WORK-team is the optional, pane-backed work-execution + context-
specialisation layer that sits ON TOP of the loops (which stay on the lead
session — no loop ever lives on a teammate). It declares three roles —
CORE-MAKER, OVERLAY-MAKER, REVIEWER — each with a canonical ``claimed_by``
key in the ``team:<role>`` namespace (disjoint from the t3-master / per-loop /
infra slots), and each maker role a declarative overlay-seam claim filter that
partitions the backlog (CORE → ``overlay == ""``, OVERLAY → ``overlay != ""``).

The registry ships DARK behind the default-OFF ``teams_enabled`` toggle: the
module is PURE DATA, imports nothing from ``teatree`` (a foundation leaf), and is
referenced by NOTHING in the loop / dispatch / claim execution path.

**No production caller spawns a teammate pane (#3734).** The whole package is
built and tested, but the only live-path importer is the idle-pane reaper
scanner, which consumes four reaper-side symbols. ``claim_maker_pane``,
``TeammatePane.spawn``, ``build_pane_options``, ``spawn_pane``, the guardrails
and the role claim filters have zero references outside this package and its
tests — so ``t3 teams on`` produces no pane, and the reaper reaps nothing because
nothing claims a ``team:*`` slot. Whether to wire the spawn half in or retire it
is an open owner decision.
"""

from teatree.teams.roles import TEAM_CLAIM_PREFIX, TeamRole, is_team_claim_slot, team_claim_slot

__all__ = [
    "TEAM_CLAIM_PREFIX",
    "TeamRole",
    "is_team_claim_slot",
    "team_claim_slot",
]

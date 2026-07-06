"""The behavior-probe catalog — VERIFYING evidence class 3 (north-star PR-7).

A behavior probe answers "does the constraint the directive activated actually
HOLD in production?" over the verify horizon. Each is a pure-ish query keyed by a
dotted path in :data:`PROBE_CATALOG`; a directive's ``MechanismSketch.behavior_probe``
names one, and :func:`resolve_probe` looks it up. A probe returns a finding string
(the violation, naming the offending object) or ``None`` when the behavior holds.

Day one carries one entry — :func:`pr_budget_violations`, the proof-case probe for
``max_open_prs_per_repo_per_ticket``. A directive whose constraint has no probe
records a ``probe_none_reason`` and leans on the other four evidence classes; the
catalog grows a probe per new directive kind, never a special-case in the loop.
"""

from collections.abc import Callable
from datetime import datetime

from teatree.core.gates.pr_budget_gate import count_open_prs_for_repo, resolve_pr_budget
from teatree.core.models import PullRequest, Ticket

#: A probe reads ``(activation scope, since)`` and returns a finding or ``None``.
BehaviorProbe = Callable[[str, datetime], str | None]


def pr_budget_violations(scope: str, since: datetime) -> str | None:
    """Any ``(ticket, repo)`` in *scope* breaching the open-PR budget, or ``None`` when clean.

    The proof-case probe. Enumerates the distinct ``(ticket, repo)`` pairs with open
    PRs in *scope* and re-counts each through the PR-2 gate's own union query (FK rows
    unioned with the synchronously-written ``extra`` index), flagging the first pair
    whose open count exceeds the effective limit. ``since`` frames the finding: the constraint is
    a *current-state* invariant ("at most N open at once"), and ``PullRequest`` rows
    carry no open-timestamp to filter on, so the check reads the live open set rather
    than a historical window.
    """
    limit = resolve_pr_budget(scope or None)
    if limit <= 0:
        return None
    pairs = (
        PullRequest.objects.filter(overlay=scope)
        .exclude(state=PullRequest.State.MERGED)
        .values_list("ticket_id", "repo")
        .distinct()
    )
    for ticket_id, repo in pairs:
        ticket = Ticket.objects.filter(pk=ticket_id).first()
        if ticket is None:
            continue
        count = count_open_prs_for_repo(ticket, repo)
        if count > limit:
            return (
                f"ticket {ticket.ticket_number or ticket_id} has {count} open PRs in {repo!r} "
                f"(limit {limit}) since {since:%Y-%m-%d}"
            )
    return None


#: The dotted-path registry a sketch's ``behavior_probe`` field names.
PROBE_CATALOG: dict[str, BehaviorProbe] = {
    "pr_budget_violations": pr_budget_violations,
}


def resolve_probe(dotted: str) -> BehaviorProbe | None:
    """The catalog probe *dotted* names, or ``None`` when it is empty or unknown."""
    return PROBE_CATALOG.get(dotted.strip()) if dotted.strip() else None

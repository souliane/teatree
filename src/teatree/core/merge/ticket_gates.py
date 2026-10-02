"""The two ticket-scoped merge gates, run by PR identity at the shared chokepoint.

The anti-vacuity attestation (#1829) and the rubric done-gate (#2241) are recorded on
the TICKET, and their CLEAR-taking twins in :mod:`authorization` therefore only ever
ran on the keystone path. The solo-overlay bypass and the uv-audit raw fallback reach
the forge through :func:`~teatree.core.merge.execution.execute_bound_merge` with no
CLEAR at all, so they merged ungraded by both — on installs that had turned the
settings ON, with nothing telling the operator the gate had not run. Resolving the
ticket from the PR identity instead makes both gates reachable from the one chokepoint
every merge path crosses.

A PR no ticket owns is OUTSIDE the rubric gate's subject. ``pr create`` records every
factory PR on the ``PullRequest`` ledger with its owning ticket, and a keystone CLEAR
carries one; so an unresolvable ticket means a PR the factory did not author, with no
plan to grade and no ticket a bypass could be recorded on. The setting-scoped
anti-vacuity refusal still fires there — that one has an operator remedy.
"""

import logging

from teatree.core.gates import anti_vacuity_gate, rubric_gate
from teatree.core.gates.anti_vacuity_gate import AntiVacuityAttestationError
from teatree.core.gates.rubric_gate import RubricNotSatisfiedError
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.substrate_standing import resolve_overlay_by_repo_identity
from teatree.core.merge.ticket_resolution import resolve_gated_ticket

logger = logging.getLogger(__name__)


def assert_ticket_scoped_gates(*, slug: str, pr_id: int, head_sha: str) -> None:
    """Run the anti-vacuity + rubric gates at the SHARED merge chokepoint, by PR identity.

    The ticket is resolved through the SAME
    :func:`~teatree.core.merge.ticket_resolution.resolve_gated_ticket` the sibling
    quality gate already calls at this chokepoint (PR ledger first, CLEAR second, both
    case-insensitive) — a second resolver here is how the four-way slug divergence got
    built, so there is deliberately only one.

    Both gates are recorded ON a ticket, so an unresolvable one is a PR this factory
    did not author: ``pr create`` puts every factory PR on the ``PullRequest`` ledger
    with its ticket, and a keystone CLEAR carries one. Such a PR has no plan to grade
    and no ticket a bypass could be recorded on, so refusing it is a lockout with no
    escape rather than a gate — the rubric gate SKIPS it, loudly. The anti-vacuity refusal is
    setting-scoped and unchanged: in force means refuse.
    """
    ticket = resolve_gated_ticket(slug=slug, pr_id=pr_id)
    if ticket is None:
        logger.warning(
            "merge of %s#%s is outside the rubric gate: no owning ticket resolves, so nothing is graded", slug, pr_id
        )
        overlay = resolve_overlay_by_repo_identity(slug, fallback="") or None
        if not anti_vacuity_gate.anti_vacuity_required(overlay):
            return
        msg = (
            f"require_anti_vacuity_attestation is in force but no owning ticket resolves for {slug}#{pr_id} — "
            f"it is recorded on the ticket, so the gate cannot be evaluated and the merge is "
            f"refused rather than merged silently ungraded. Link the PR to its ticket "
            f"(`t3 <overlay> ticket backfill-clears`, or re-issue the CLEAR with `--ticket-id <id>`)."
        )
        raise MergePreconditionError(msg)
    try:
        anti_vacuity_gate.check_anti_vacuity_attestation(ticket, head_sha, transition="merge")
        rubric_gate.check_rubric_satisfied(ticket, head_sha, transition="merge")
    except (AntiVacuityAttestationError, RubricNotSatisfiedError) as exc:
        raise MergePreconditionError(str(exc)) from exc

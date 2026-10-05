"""The two ticket-scoped merge gates, run by PR identity at the shared chokepoint.

The anti-vacuity attestation (#1829) and the rubric done-gate (#2241) are recorded on
the TICKET, and their CLEAR-taking twins in :mod:`authorization` therefore only ever
ran on the keystone path. The solo-overlay bypass and the uv-audit raw fallback reach
the forge through :func:`~teatree.core.merge.execution.execute_bound_merge` with no
CLEAR at all, so they merged ungraded by both. Resolving the
ticket from the PR identity instead makes both gates reachable from the one chokepoint
every merge path crosses.

A PR no ticket owns is outside these ticket-scoped gates and keeps the upstream
merge path. The review, provenance, draft, lock, and CI guards still apply.
"""

import logging

from teatree.core.gates import anti_vacuity_gate, rubric_gate
from teatree.core.gates.anti_vacuity_gate import AntiVacuityAttestationError
from teatree.core.gates.rubric_gate import RubricNotSatisfiedError
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.ticket_resolution import resolve_gated_ticket

logger = logging.getLogger(__name__)


def assert_ticket_scoped_gates(*, slug: str, pr_id: int, head_sha: str) -> None:
    """Run the anti-vacuity + rubric gates at the SHARED merge chokepoint, by PR identity.

    The ticket is resolved through the SAME
    :func:`~teatree.core.merge.ticket_resolution.resolve_gated_ticket` the sibling
    quality gate already calls at this chokepoint (PR ledger first, CLEAR second, both
    case-insensitive) — a second resolver here is how the four-way slug divergence got
    built, so there is deliberately only one.


    Both gates are recorded on a ticket. A PR with no owning ticket has no ticket
    evidence to grade, so these two gates return after logging its scope.
    """
    ticket = resolve_gated_ticket(slug=slug, pr_id=pr_id)
    if ticket is None:
        logger.warning("merge of %s#%s is outside the ticket-scoped gates: no owning ticket resolves", slug, pr_id)
        return
    try:
        anti_vacuity_gate.check_anti_vacuity_attestation(ticket, head_sha, transition="merge")
        rubric_gate.check_rubric_satisfied(ticket, head_sha, transition="merge")
    except (AntiVacuityAttestationError, RubricNotSatisfiedError) as exc:
        raise MergePreconditionError(str(exc)) from exc

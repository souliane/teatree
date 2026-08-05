"""The two ticket-scoped merge gates, run by PR identity at the shared chokepoint.

The anti-vacuity attestation (#1829) and the rubric done-gate (#2241) are recorded on
the TICKET, and their CLEAR-taking twins in :mod:`authorization` therefore only ever
ran on the keystone path. The solo-overlay bypass and the uv-audit raw fallback reach
the forge through :func:`~teatree.core.merge.execution.execute_bound_merge` with no
CLEAR at all, so they merged ungraded by both — on installs that had turned the
settings ON, with nothing telling the operator the gate had not run. Resolving the
ticket from the PR identity instead makes both gates reachable from the one chokepoint
every merge path crosses.
"""

from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.substrate_standing import resolve_overlay_by_repo_identity


def assert_ticket_scoped_gates(*, slug: str, pr_id: int, head_sha: str) -> None:
    """Run the anti-vacuity + rubric gates at the SHARED merge chokepoint, by PR identity.

    The ticket is resolved through the SAME
    :func:`~teatree.core.gates.merge_quality_gate._resolve_gated_ticket` the sibling
    quality gate already calls at this chokepoint (PR ledger first, CLEAR second, both
    case-insensitive) — a second resolver here is how the four-way slug divergence got
    built, so there is deliberately only one.

    An unresolvable ticket while either setting is IN FORCE REFUSES the merge. Skipping
    is the shape the operator cannot see: they turn the setting on, nothing resolves a
    ticket, and the gate they enabled grades nothing while every signal reads healthy.
    An ungradable subject is not evidence that the subject is satisfied. With both
    settings off (the shipped default) an unresolvable ticket is a plain no-op, so
    nothing that merges today stops merging.
    """
    from teatree.core.gates import merge_quality_gate  # noqa: PLC0415 avoids a core.merge/core.gates cycle
    from teatree.core.gates.anti_vacuity_gate import (  # noqa: PLC0415 — deferred: call-time import, kept lazy
        AntiVacuityAttestationError,
        anti_vacuity_required,
        check_anti_vacuity_attestation,
    )
    from teatree.core.gates.rubric_gate import (  # noqa: PLC0415 — deferred: call-time import, kept lazy
        RubricNotSatisfiedError,
        check_rubric_satisfied,
        rubric_gate_required,
    )

    ticket = merge_quality_gate._resolve_gated_ticket(slug=slug, pr_id=pr_id)  # noqa: SLF001 — one resolver, not two
    if ticket is None:
        overlay = resolve_overlay_by_repo_identity(slug, fallback="") or None
        required = [
            name
            for name, in_force in (
                ("require_anti_vacuity_attestation", anti_vacuity_required(overlay)),
                ("require_rubric_verification", rubric_gate_required(overlay)),
            )
            if in_force
        ]
        if required:
            msg = (
                f"{' and '.join(required)} enabled but no owning ticket resolves for {slug}#{pr_id} — "
                f"both are recorded on the ticket, so the gate cannot be evaluated and the merge is "
                f"refused rather than merged silently ungraded. Link the PR to its ticket "
                f"(`t3 <overlay> ticket backfill-clears`, or re-issue the CLEAR with `--ticket-id <id>`)."
            )
            raise MergePreconditionError(msg)
        return
    try:
        check_anti_vacuity_attestation(ticket, head_sha, transition="merge")
        check_rubric_satisfied(ticket, head_sha, transition="merge")
    except (AntiVacuityAttestationError, RubricNotSatisfiedError) as exc:
        raise MergePreconditionError(str(exc)) from exc

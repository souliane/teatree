"""plan_currency FSM gate: coding is unreachable without an adequate, current-HEAD-bound plan.

The named root cause of the 26-bug integration campaign was two closely-related
holes the plan-first gate did NOT close: (1) a thin scope+acceptance SPEC passed
as a plan because ``check_plan_artifact`` only checked ``plan_text.strip()``
non-empty, and (2) a plan authored against a weeks-stale base authorized coding
forever because ``PlanArtifact`` carried no base SHA. This gate forecloses both,
structurally, before any coder runs.

It mirrors ``anti_vacuity_gate`` file-for-file:

``is_adequate``
    the plan's four-section manifest (design, integration_seams, edge_cases,
    test_strategy) is complete — each section substantive OR an explicit reasoned
    negative. Silence never passes. (The pure validator lives in
    ``models.plan_adequacy``; re-exported here so the gate reads like its sibling.)

``is_bound_to``
    the plan's ``base_sha`` equals the live target HEAD (the SHA bind), mirroring
    ``MergeClear.reviewed_sha`` / ``anti_vacuity_gate.is_bound_to``.

``check_plan_current``
    the gate: when ``require_plan_adequacy`` is on, the latest plan must be adequate
    AND current. STALE-IS-ABSENT — a plan whose base moved off the live target HEAD
    and whose intervening commits touch a DECLARED integration seam (computed via
    ``git log base_sha..HEAD -- <seam paths>`` through ``branch_currency``'s fetch
    machinery) is treated as ABSENT, exactly the replay-closure semantics of the
    anti-vacuity/rubric SHA-binds. On a block it raises :class:`NoCurrentPlanError`
    naming the ``plan-reaffirm`` remediation — never a hard lock.

Wired as a SECOND condition on ``Ticket.code()`` (PLANNED→CODED) AND called at the
top of ``Ticket.schedule_coding()`` — closing the coder-dispatch leak where a
coding task is minted outside the ``code()`` transition. ``require_plan_adequacy``
ships OFF (opt-in) so the generic FSM is never blocked; it is a NO-OP until an
operator flips it on per-overlay.

Fail-open on an inconclusive probe (no worktree, failed fetch, unresolvable range)
— same network posture as ``branch_currency``/``clone_guard``: a network outage
must not wedge coding. The teeth are on the deterministic stale-on-a-seam case.
"""

from typing import TYPE_CHECKING

from teatree.core.branch_currency import commits_between_touching_paths, fetch_target_head
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.errors import NoCurrentPlanError
from teatree.core.models.plan_adequacy import declared_seam_paths, is_adequate
from teatree.core.models.plan_artifact import PlanArtifact, plan_adequacy_required
from teatree.core.models.ticket_worktree_checks import _resolve_base_branch, dispatch_worktree_path
from teatree.core.models.trivial_plan_skip import is_trivial_plan_skip

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def latest_plan_artifact(ticket: "Ticket") -> "PlanArtifact | None":
    """The governing (latest) plan artifact for *ticket*, or ``None`` when it has none.

    The append-only model orders ``-recorded_at``, so ``.first()`` is the latest —
    the one a ``plan-reaffirm`` appends and this gate reads.
    """
    return PlanArtifact.objects.filter(ticket=ticket).order_by("-recorded_at", "-pk").first()


def is_bound_to(artifact: "PlanArtifact", head_sha: str) -> bool:
    """Whether *artifact*'s ``base_sha`` equals *head_sha* (the SHA bind).

    Case-insensitive on the stripped SHA so a mixed-case ``head_sha`` from a forge
    ``headRefOid`` cannot silently miss. An empty recorded or presented SHA never
    matches — a legacy blank-base artifact is stale by construction.
    """
    recorded = (artifact.base_sha or "").strip().lower()
    presented = (head_sha or "").strip().lower()
    return bool(recorded) and bool(presented) and recorded == presented


def check_plan_current(ticket: "Ticket") -> bool:
    """Return True iff *ticket* may leave PLANNED for CODED (or mint a coding task).

    A django-fsm ``@transition`` condition — returns a bool, and raises
    :class:`NoCurrentPlanError` (an ``InvalidTransitionError`` subclass) on a block
    so callers get a typed exception with the ``plan-reaffirm`` remediation.

    NO-OP when ``require_plan_adequacy`` is off (the opt-in default) or when a
    trivial-skip marker carries the ticket (a trivial mechanical edit has no plan
    or seams to bind). Otherwise the latest plan must be ADEQUATE and CURRENT;
    inconclusive probes (no worktree, failed fetch, unresolvable range) fail OPEN.
    """
    overlay = getattr(ticket, "overlay", "") or None
    if not plan_adequacy_required(overlay):
        return True
    if is_trivial_plan_skip(ticket):
        return True

    artifact = latest_plan_artifact(ticket)
    if artifact is None:
        # No plan at all — absence is the plan-first gate's (plan()) concern, not
        # this one's; do not introduce a second absence-block. Currency is moot.
        return True
    if not is_adequate(artifact.adequacy):
        raise NoCurrentPlanError(_inadequate_reason(ticket))

    stale = _detect_stale_on_seam(ticket, artifact)
    if stale is not None:
        head, seam_commits = stale
        seams = declared_seam_paths(artifact.adequacy)
        raise NoCurrentPlanError(_stale_reason(ticket, artifact.base_sha, head, seams, seam_commits))
    return True


def _detect_stale_on_seam(ticket: "Ticket", artifact: "PlanArtifact") -> tuple[str, tuple[str, ...]] | None:
    """``(head, seam_commits)`` when the plan is DEFINITIVELY stale on a seam, else ``None``.

    ``None`` covers both "current" and every inconclusive fail-open case (no
    worktree, failed fetch, unresolvable range) — currency the probe cannot
    determine never blocks. A non-``None`` result is the deterministic teeth: the
    base moved off the live HEAD AND an intervening commit touched a declared seam.
    """
    repo = dispatch_worktree_path(ticket)
    if not repo:
        return None  # no materialised worktree — undeterminable, fail open
    head = fetch_target_head(repo, _resolve_base_branch(repo))
    if not head or is_bound_to(artifact, head):
        return None  # inconclusive fetch, or exactly bound to the live HEAD
    seams = declared_seam_paths(artifact.adequacy)
    seam_commits = commits_between_touching_paths(repo, artifact.base_sha, head, seams)
    if not seam_commits:
        return None  # None (inconclusive) or () (moved but no declared seam touched)
    return head, seam_commits


def _inadequate_reason(ticket: "Ticket") -> str:
    # An INADEQUATE plan cannot be fixed by carrying it forward — the remediation is
    # to SUPPLY a manifest (or bypass / disable), distinct from the STALE case below.
    return (
        f"Refusing to advance ticket {ticket.pk} to CODED — its latest plan is not adequate "
        f"(require_plan_adequacy). A plan must carry a complete four-section manifest "
        f"(design, integration_seams, edge_cases, test_strategy). A legacy/thin plan is treated as absent. "
        f"Supply a real manifest (which also re-binds the base) with "
        f"`t3 <overlay> ticket plan-reaffirm {ticket.pk} --base-sha <current-40-char-HEAD> "
        f"--adequacy-json '<four-section manifest>'`, OR record an audited bypass "
        f"(`t3 <overlay> ticket plan-bypass {ticket.pk} --human-authorize <who> --reason <why>`), OR "
        f"disable the gate (`t3 <overlay> config_setting set require_plan_adequacy false --overlay <name>`)."
    )


def _stale_reason(ticket: "Ticket", base_sha: str, head: str, seams: tuple[str, ...], commits: tuple[str, ...]) -> str:
    # A STALE-but-adequate plan IS fixable by carrying it forward — the remediation
    # is reaffirm + a per-intervening-commit disposition. The printed `--base-sha` is
    # the full 40-char HEAD so the command is copy-paste runnable (reaffirm rejects a
    # short SHA).
    return (
        f"Refusing to advance ticket {ticket.pk} to CODED — its plan is STALE (require_plan_adequacy). "
        f"The plan was authored against base {base_sha[:8]} but the target HEAD is now {head[:8]}, and "
        f"{len(commits)} intervening commit(s) touched a declared integration seam "
        f"({', '.join(seams) or '<none>'}). A stale plan is treated as ABSENT — coding against a moved "
        f"base is the named root cause of the integration-bug campaign. Reaffirm at the new base with "
        f"`t3 <overlay> ticket plan-reaffirm {ticket.pk} --base-sha {head} --disposition <per-commit "
        f"disposition>`, or disable the gate: "
        f"`t3 <overlay> config_setting set require_plan_adequacy false --overlay <name>`."
    )


register_gate("plan_currency", check_plan_current)

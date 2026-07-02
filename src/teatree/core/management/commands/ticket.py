"""Ticket state management: transitions and listing for the loop and CLI."""

import logging
from typing import Annotated, TypedDict

import typer
from django.db import transaction
from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command

from teatree.core.gates.owned_repo_guard import MergeKeystoneResult, escalated_merge_result, merge_clear_refusal
from teatree.core.gates.schema_guard import SelfDbMigrationError, require_current_schema
from teatree.core.management.commands._clear_preflight import clear_preflight_refusal
from teatree.core.management.commands._context_commands import ContextCommands
from teatree.core.management.commands._plan_gate_commands import (
    PlanAdvanceError,
    PlanReconcileResult,
    PlanResult,
    reconcile_inflight,
    record_artifact_and_advance,
    record_trivial_skip_and_advance,
)
from teatree.core.management.commands._rubric_commands import RubricCommands
from teatree.core.management.commands._ticket_show import TicketShowCommands
from teatree.core.management.commands._transition_refusals import review_context_refusal
from teatree.core.merge import MergePreconditionError, merge_ticket_pr, resolve_pr_repo_slug
from teatree.core.models import ClearIssuanceError, ClearRequest, MergeClear, ReviewVerdict, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.external_delivery import refresh_external_delivery_if_active


class CompletionResult(TypedDict, total=False):
    ticket_id: int
    issue_url: str
    from_state: str
    to_state: str
    action: str


class CommentResult(TypedDict, total=False):
    issue_url: str
    comment_id: int
    error: str


class CreateSubResult(TypedDict, total=False):
    parent_url: str
    child_iid: int
    child_url: str
    error: str


class ClearIssueResult(TypedDict, total=False):
    issued: bool
    clear_id: int
    pr_id: int
    slug: str
    blast_class: str
    human_authorizer: str
    ticket_id: int
    recorded_verdict_id: int
    error: str


class DodOverrideResult(TypedDict, total=False):
    ticket_id: int
    reason: str
    by: str
    at: str


class E2EBypassResult(TypedDict, total=False):
    recorded: bool
    error: str
    ticket_id: int
    head_sha: str
    approver: str


class ReattributeResult(TypedDict, total=False):
    ticket_id: int
    issue_url: str
    from_overlay: str
    to_overlay: str
    action: str


logger = logging.getLogger(__name__)

_ALLOWED_TRANSITIONS = {
    "scope",
    "start",
    "plan",
    "code",
    "test",
    "review",
    "ship",
    "request_review",
    "mark_merged",
    "retrospect",
    "mark_delivered",
    "rework",
    # #1077: reviewer concludes an external review with no postable/
    # approvable action — terminal disposition for the reviewing task.
    "mark_review_no_action",
    # #1118: phase-driven catch-up to REVIEWED. The FSM exposes it via
    # ``get_available_FIELD_transitions`` from every non-terminal state
    # (#808); the CLI must mirror the FSM-table surface so a ticket
    # stranded at ``in_review`` after a failed ship can be reconciled
    # without a code-level workaround.
    "reconcile_reviewed",
}


class Command(RubricCommands, TicketShowCommands, ContextCommands, TyperCommand):
    @command()
    def transition(self, ticket_id: int, transition_name: str) -> dict[str, object]:
        """Transition a ticket to a new state.

        Accepts any of the allowed transition names: scope, start, code, test,
        review, ship, request_review, mark_merged, retrospect, mark_delivered,
        rework, mark_review_no_action.
        """
        if transition_name not in _ALLOWED_TRANSITIONS:
            return {"error": f"Unknown transition: {transition_name}"}

        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            return {"error": f"Ticket {ticket_id} not found"}

        method = getattr(ticket, transition_name, None)
        if method is None:
            return {"error": f"Invalid transition: {transition_name}"}

        try:
            with transaction.atomic():
                method()
                ticket.save()
                # #2217: external-owner FSM seam — refresh a LIVE lease so a long
                # hand delivery never lapses mid-delivery (no-op without one).
                refresh_external_delivery_if_active(ticket)
        except TransitionNotAllowed:
            # Surface the deep-retrieval refusal reason when that blocked the
            # `review` transition, else the generic not-allowed message.
            context_refusal = review_context_refusal(ticket, transition_name)
            generic = f"Transition '{transition_name}' not allowed from state '{ticket.state}'"
            return {"error": context_refusal or generic}
        except InvalidTransitionError as exc:
            # Dirty-worktree / missing-E2E DoD refusals: the FSM stays put
            # (the gate keeps blocking) and the refusal reason is surfaced
            # to the caller instead of a raw traceback.
            return {"error": f"Transition '{transition_name}' refused from state '{ticket.state}': {exc}"}

        return {"ticket_id": int(ticket.pk), "state": ticket.state}

    @command(name="dod-override")
    def dod_override(
        self,
        ticket_id: int,
        *,
        reason: Annotated[
            str,
            typer.Option(help="Why this UI-visible ticket may ship without a local-stack E2E (#88)."),
        ],
        by: Annotated[
            str,
            typer.Option(help="Who is recording the override (audit trail)."),
        ] = "",
    ) -> DodOverrideResult:
        """Record the DoD local-E2E gate escape hatch for a ticket (#88).

        The gate refuses to ship a UI-visible ticket without a green
        local-stack E2E artifact. This records an explicit, audited override
        so a genuinely non-UI or exempt ticket the heuristic mis-flags can
        still ship — the gate can never hard-trap a legitimate ticket. A
        blank ``--reason`` is refused: a silent bypass is exactly what #88
        forecloses.
        """
        from django.utils import timezone  # noqa: PLC0415

        from teatree.core.models.types import DodE2EOverride  # noqa: PLC0415

        cleaned = reason.strip()
        if not cleaned:
            self.stderr.write("  refused: --reason is required (a silent DoD-gate bypass is not allowed).")
            raise SystemExit(1)
        ticket = self._resolve_ticket(ticket_id)
        recorded_at = timezone.now().isoformat()
        ticket.merge_extra(set_keys={"dod_e2e_override": DodE2EOverride(reason=cleaned, by=by.strip(), at=recorded_at)})
        self.stdout.write(f"  DoD local-E2E gate override recorded for ticket {ticket.pk}")
        return DodOverrideResult(ticket_id=int(ticket.pk), reason=cleaned, by=by.strip(), at=recorded_at)

    @command()
    def plan(
        self,
        ticket_id: int,
        plan_text: Annotated[str, typer.Argument(help="The plan text recorded as the PlanArtifact.")],
        *,
        recorded_by: Annotated[
            str,
            typer.Option(help="Author identity recorded on the artifact (audit trail)."),
        ] = "operator",
    ) -> PlanResult:
        """Record a PlanArtifact and advance the ticket STARTED → PLANNED.

        The operator-facing plan recorder named by the ``NoPlanArtifactError``
        message: a planning task that finished out-of-band, or a ticket the
        planner never ran on, advances by recording the plan here. A blank
        ``plan_text`` is refused — a vacuous artifact cannot advance the FSM. For
        an *audited bypass* (no real plan, explicit human sign-off) use
        ``plan-bypass``; for a trivial mechanical edit use ``skip-planning``.
        """
        cleaned_text = plan_text.strip()
        if not cleaned_text:
            self.stderr.write("  refused: plan_text is required (a vacuous plan cannot advance the FSM)")
            raise SystemExit(1)

        ticket = self._resolve_ticket(ticket_id)
        try:
            artifact = record_artifact_and_advance(
                ticket=ticket, plan_text=cleaned_text, recorded_by=recorded_by.strip() or "operator"
            )
        except PlanAdvanceError as exc:
            return PlanResult(ticket_id=int(ticket.pk), error=exc.message)

        # #2217: external-owner FSM seam — refresh a LIVE lease (no-op without one).
        refresh_external_delivery_if_active(ticket)
        self.stdout.write(f"  plan recorded for ticket {ticket.pk} (artifact {artifact.pk}); state → {ticket.state}")
        return PlanResult(ticket_id=int(ticket.pk), artifact_id=int(artifact.pk), state=ticket.state)

    @command(name="e2e-bypass")
    def e2e_bypass(
        self,
        ticket_id: int,
        *,
        approver: Annotated[
            str,
            typer.Option(
                "--approver",
                help="Human user id authorising the bypass; a maker/coding-agent/loop id is refused (#1967).",
            ),
        ],
        head_sha: Annotated[
            str,
            typer.Option("--head-sha", help="Full 40-char hex SHA of the reviewed tree the bypass authorises."),
        ],
    ) -> "E2EBypassResult":
        """Record a single-use user bypass of the mandatory-E2E gate (#1967).

        The ONLY way past the mandatory-E2E gate without recorded green E2E
        evidence — and it requires explicit user approval, never the
        implementing agent's own judgment. Mirrors ``OnBehalfApproval`` /
        ``MergeClear``: durable, single-use, scoped to the ticket + reviewed
        head SHA, maker≠checker enforced (a maker/coding-agent/loop ``--approver``
        is refused). The next ship-gate / §17.4 CLEAR evaluation at that exact
        SHA consumes it once.
        """
        from teatree.core.models.e2e_bypass import E2EBypassApproval, E2EBypassApprovalError  # noqa: PLC0415

        ticket = self._resolve_ticket(ticket_id)
        try:
            approval = E2EBypassApproval.record(ticket=ticket, head_sha=head_sha, approver_id=approver)
        except E2EBypassApprovalError as exc:
            self.stderr.write(f"  e2e-bypass refused: {exc}")
            return {"recorded": False, "error": str(exc)}
        self.stdout.write(
            f"  E2E bypass recorded for ticket {ticket.pk} @ {approval.head_sha[:8]} by {approval.approver_id}"
        )
        return {
            "recorded": True,
            "ticket_id": int(ticket.pk),
            "head_sha": approval.head_sha,
            "approver": approval.approver_id,
        }

    @command(name="plan-bypass")
    def plan_bypass(
        self,
        ticket_id: int,
        *,
        human_authorize: Annotated[
            str,
            typer.Option(
                "--human-authorize",
                help="Username of the human explicitly authorising this plan bypass.",
            ),
        ],
        reason: Annotated[
            str,
            typer.Option(help="Documented reason for bypassing the plan gate (required)."),
        ],
    ) -> PlanResult:
        """Record an audited PlanArtifact bypass and advance the ticket to PLANNED.

        The ONLY escape from the plan gate outside the normal planner flow.
        Both --human-authorize and --reason are required; a silent bypass is
        not allowed. Records a PlanArtifact with bypass_reason set, then
        drives ticket.plan() → STARTED→PLANNED.
        """
        cleaned_reason = reason.strip()
        cleaned_authorizer = human_authorize.strip()
        if not cleaned_authorizer:
            self.stderr.write("  refused: --human-authorize is required")
            raise SystemExit(1)
        if not cleaned_reason:
            self.stderr.write("  refused: --reason is required (a silent plan bypass is not allowed)")
            raise SystemExit(1)

        ticket = self._resolve_ticket(ticket_id)
        try:
            artifact = record_artifact_and_advance(
                ticket=ticket,
                plan_text=f"[audited bypass by {cleaned_authorizer}] {cleaned_reason}",
                recorded_by=cleaned_authorizer,
            )
        except PlanAdvanceError as exc:
            return PlanResult(ticket_id=int(ticket.pk), error=exc.message)

        self.stdout.write(
            f"  plan bypass recorded for ticket {ticket.pk} "
            f"(artifact {artifact.pk}, authorizer={cleaned_authorizer}); state → {ticket.state}"
        )
        return PlanResult(ticket_id=int(ticket.pk), artifact_id=int(artifact.pk), state=ticket.state)

    @command(name="skip-planning")
    def skip_planning(
        self,
        ticket_id: int,
        *,
        reason: Annotated[
            str,
            typer.Option(help="Why this ticket is a trivial mechanical edit that may skip planning (required)."),
        ],
        by: Annotated[
            str,
            typer.Option(help="Who recorded the skip (audit trail)."),
        ] = "operator",
    ) -> PlanResult:
        """Mark a trivial ticket to skip planning and advance STARTED → PLANNED.

        The LIGHTWEIGHT, audited sibling of ``plan-bypass`` for a trivial
        mechanical edit (a typo, a one-line bump): records a durable
        ``trivial_plan_skip`` marker (NO ``PlanArtifact``, no ``--human-authorize``)
        that ``check_plan_artifact`` accepts and ``execute_provision`` reads to
        skip the auto-planner. ``--reason`` is mandatory — an unreasoned skip is
        refused and records nothing. See ``models.trivial_plan_skip``.
        """
        cleaned_reason = reason.strip()
        if not cleaned_reason:
            self.stderr.write("  refused: --reason is required (an unreasoned plan skip is not allowed)")
            raise SystemExit(1)

        ticket = self._resolve_ticket(ticket_id)
        try:
            record_trivial_skip_and_advance(ticket=ticket, reason=cleaned_reason, by=by.strip() or "operator")
        except PlanAdvanceError as exc:
            return PlanResult(ticket_id=int(ticket.pk), error=exc.message)

        self.stdout.write(
            f"  trivial plan skip recorded for ticket {ticket.pk} (reason={cleaned_reason!r}); state → {ticket.state}"
        )
        return PlanResult(ticket_id=int(ticket.pk), state=ticket.state)

    @command(name="plan-reconcile-inflight")
    def plan_reconcile_inflight(
        self,
        *,
        human_authorize: Annotated[
            str,
            typer.Option(
                "--human-authorize",
                help="Human/operator authorising retroactive plan bypass for in-flight STARTED tickets.",
            ),
        ],
        issue_ref: Annotated[
            str,
            typer.Option(help="Issue/PR reference identifying why this reconcile is necessary."),
        ] = "",
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="List affected tickets without modifying them.")
        ] = False,
    ) -> PlanReconcileResult:
        """Retroactively advance STARTED tickets to PLANNED after the gate was added.

        One-time operator command (a data migration would fabricate an authorizer
        it cannot name): see ``_plan_gate_commands.reconcile_inflight``. Requires
        --human-authorize; --dry-run inspects which tickets would be affected.
        """
        cleaned_authorizer = human_authorize.strip()
        if not cleaned_authorizer:
            self.stderr.write("  refused: --human-authorize is required")
            raise SystemExit(1)

        result, log = reconcile_inflight(authorizer=cleaned_authorizer, issue_ref=issue_ref, dry_run=dry_run)
        for line in log:
            self.stdout.write(line)
        return result

    def _resolve_ticket(self, ticket_id: int) -> Ticket:
        """Fetch a ticket or abort the subcommand with a nonzero exit (#932).

        A missing ticket is a real failure — returning an ``{"error": …}``
        dict would print and exit 0, so a scripted ``ticket context`` caller
        could not tell success from "ticket not found". ``raise SystemExit(1)``
        is the sibling refusal convention (AGENTS.md § Test-Writing Doctrine).
        """
        try:
            return Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  Ticket {ticket_id} not found")
            raise SystemExit(1) from None

    @command()
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def clear(  # noqa: PLR0913 — django-typer command: every param is a CLI flag mapped 1:1 to a §17.4.2 CLEAR field; the arg list IS the public CLI surface (same rationale as the file-wide PLR6301 ignore), not an internal design smell.
        self,
        pr_id: int,
        slug: str,
        *,
        reviewed_sha: Annotated[str, typer.Option("--reviewed-sha", help="Hex commit id (§17.4.2).")] = "",
        reviewer_identity: Annotated[
            str,
            typer.Option(
                help="Independent cold reviewer identity (NOT a maker/coding-agent/loop role — §17.8 clause 3)."
            ),
        ] = "",
        gh_verify_result: Annotated[
            str,
            typer.Option(help="Audit-only snapshot of gh checks at review time: green / pending / failed."),
        ] = "green",
        blast_class: Annotated[
            str,
            typer.Option(help="Orchestrator judgment: substrate / logic / docs (§17.4.2)."),
        ] = "logic",
        ticket_id: Annotated[
            int,
            typer.Option(help="Optional teatree Ticket id this CLEAR authorises the merge for."),
        ] = 0,
        human_authorize: Annotated[
            str,
            typer.Option(
                help="ONLY for blast_class=substrate: the human/owner id authorising the substrate merge.",
            ),
        ] = "",
        executing_loop_identity: Annotated[
            str,
            typer.Option(
                help="The loop that will execute the merge; the reviewer must differ (§17.8 clause 3).",
            ),
        ] = "merge-loop",
    ) -> ClearIssueResult:
        """Issue a per-diff CLEAR — the orchestrator's only merge output (BLUEPRINT §17.4.2).

        Records the orchestrator's reviewed/verified judgment as a durable
        ``MergeClear`` row the durable loop later acts on by id via
        ``ticket merge``. This is the missing issuance seam: #863 added the
        consume side but no command created the row. The CLEAR is the
        compaction-surviving handoff — the orchestrator may be restarted
        before the loop picks it up, so it lives in the DB, not a session
        file.

        §17.8 clause 3 is enforced here: ``--reviewer-identity`` must name an
        independent cold reviewer — a maker/coding-agent/loop role is refused
        (the author cannot rubber-stamp their own CLEAR). ``reviewed_sha``
        must be a hex commit id (not a branch ref) so the loop can bind the
        merge to the exact reviewed tree.

        ``--human-authorize`` is valid ONLY with ``--blast-class substrate``:
        it records *who approved* a substrate merge (the gate) so the
        otherwise approval-gated / draft-locked substrate change can be
        merged BY THE AGENT through the SAME sanctioned ``ticket merge``
        transition (invariant 8 — never raw ``gh``, never a human-performed
        merge), with the human approval durably on the CLEAR.
        """
        if not reviewed_sha.strip():
            # #1231: ``--reviewed-sha`` is the canonical named option; the
            # default keeps ``call_command`` happy, this guard enforces it.
            self.stderr.write("  CLEAR refused: --reviewed-sha is required (hex commit id of the reviewed tree)")
            raise SystemExit(1)
        try:
            require_current_schema()
        except SelfDbMigrationError as exc:
            self.stdout.write(f"  CLEAR refused: {exc}")
            return {"issued": False, "error": str(exc)}

        resolved_ticket = None
        if ticket_id:
            try:
                resolved_ticket = Ticket.objects.get(pk=ticket_id)
            except Ticket.DoesNotExist:
                return {"issued": False, "error": f"Ticket {ticket_id} not found"}

        preflight_refusal = clear_preflight_refusal(reviewed_sha, resolved_ticket)
        if preflight_refusal is not None:
            self.stdout.write(f"  CLEAR refused: {preflight_refusal}")
            return {"issued": False, "error": preflight_refusal}

        request = ClearRequest(
            pr_id=pr_id,
            # Strip so the by-product verdict keys off the same normalized slug the
            # merge gate resolves — whitespace can otherwise flip _looks_like_owner_repo.
            slug=slug.strip(),
            reviewed_sha=reviewed_sha,
            reviewer_identity=reviewer_identity,
            gh_verify_result=gh_verify_result,
            blast_class=blast_class,
            ticket=resolved_ticket,
            human_authorizer=human_authorize,
            executing_loop_identity=executing_loop_identity,
        )

        # Resolve the verdict's owner/repo BEFORE issuing: resolve_pr_repo_slug
        # fails closed in a degenerate environment (workstream slug, no ticket
        # issue_url, no clone origin), and resolving it after MergeClear.issue()
        # persisted the row would orphan an already-issued CLEAR behind a traceback.
        # Issue runs only when resolution succeeds, so neither failure persists a row.
        try:
            verdict_slug = resolve_pr_repo_slug(request)
            clear = MergeClear.issue(request)
        except (MergePreconditionError, ClearIssuanceError) as exc:
            self.stdout.write(f"  CLEAR refused: {exc}")
            return {"issued": False, "error": str(exc)}

        self.stdout.write(f"  issued CLEAR {clear.pk} for {clear.slug}#{clear.pr_id}@{clear.reviewed_sha[:8]}")
        # Record the durable read-side sibling (a merge-safe judgment by
        # construction — issuance refused any non-green verdict) so a later
        # `review status` answers "safe to approve at the current head?". Key it
        # under verdict_slug — where the merge gate queries — not the workstream slug.
        verdict = ReviewVerdict.record(
            pr_id=clear.pr_id,
            slug=verdict_slug,
            reviewed_sha=clear.reviewed_sha,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity=clear.reviewer_identity,
            blast_class=clear.blast_class,
            gh_verify_result=clear.gh_verify_result,
            ticket=resolved_ticket,
        )
        result: ClearIssueResult = {
            "issued": True,
            "clear_id": int(clear.pk),
            "pr_id": int(clear.pr_id),
            "slug": clear.slug,
            "blast_class": clear.blast_class,
            "human_authorizer": clear.human_authorizer,
            "recorded_verdict_id": int(verdict.pk),
        }
        if resolved_ticket is not None:
            result["ticket_id"] = int(resolved_ticket.pk)
        return result

    @command()
    def merge(
        self,
        clear_id: int,
        *,
        loop_identity: Annotated[
            str,
            typer.Option(help="Identity of the executing loop (must differ from the CLEAR reviewer — §17.8 clause 3)."),
        ] = "merge-loop",
        human_authorized: Annotated[
            str,
            typer.Option(
                help="Substrate-only: the recorded human authoriser id, re-presented to merge a substrate CLEAR.",
            ),
        ] = "",
    ) -> MergeKeystoneResult:
        """Execute the missing IN_REVIEW → MERGED keystone transition (BLUEPRINT §17.4).

        The ONLY sanctioned merge path. Raw ``gh pr merge`` / ``glab mr
        merge`` is mechanically refused on teatree-managed tickets (the
        prohibition guard in ``hook_router``); they bypass the ledger
        update, attestation binding, and ``mark_merged()`` and leave the
        FSM incoherent.

        Pre-condition (§17.4.3): a valid, actionable ``MergeClear`` (CLI
        arg ``clear_id``), CI green on the exact PR head, an independent
        cold-review CLEAR (``reviewer_identity`` != ``--loop-identity``),
        SHA-match, not-draft, and ``blast_class`` != substrate. The merge
        is bound to ``expected_head_oid`` and fails closed on head drift.
        Post hook: atomic CLEAR-consume + ``MergeAudit`` + attestation
        binding + ``ticket.mark_merged()``.

        ``--human-authorized`` is the sanctioned substrate approval path
        (invariant 8): the loop NEVER auto-merges substrate, but the recorded
        human approval id (set on the CLEAR via ``ticket clear …
        --human-authorize``) is re-presented here and **the agent executes**
        the substrate merge through THIS SAME transition — not raw ``gh``,
        never a human-performed merge (approval is the gate, the agent is the
        executor). It cannot unlock a non-substrate CLEAR, so it can never
        bypass independent loop review of logic/docs.

        On a pre-condition failure the FSM is left untouched and the
        result is flagged ``escalated`` so the durable backlog re-escalation
        is visible (the loop never self-issues a replacement CLEAR).
        """
        try:
            require_current_schema()
        except SelfDbMigrationError as exc:
            self.stdout.write(f"  merge refused: {exc}")
            return {"error": str(exc), "merged": False}

        try:
            clear = MergeClear.objects.get(pk=clear_id)
        except MergeClear.DoesNotExist:
            return {"error": f"MergeClear {clear_id} not found", "merged": False}

        if (scope_refusal := merge_clear_refusal(clear, approved=bool(human_authorized))) is not None:
            return scope_refusal

        try:
            outcome = merge_ticket_pr(
                clear=clear,
                executing_loop_identity=loop_identity,
                human_authorized=human_authorized,
            )
        except MergePreconditionError as exc:
            self.stdout.write(f"  merge refused (re-escalating): {exc}")
            return escalated_merge_result(clear, str(exc))

        result: MergeKeystoneResult = {
            "merged": True,
            "pr_id": outcome.pr_id,
            "slug": outcome.slug,
            "merged_sha": outcome.merged_sha,
            "ticket_state": outcome.ticket_state,
        }
        if clear.ticket_id is not None:
            result["ticket_id"] = int(clear.ticket_id)
        self.stdout.write(f"  merged {outcome.slug}#{outcome.pr_id} → ticket state {outcome.ticket_state}")
        return result

    @command()
    def comment(
        self,
        issue_url: str,
        *,
        body: Annotated[str, typer.Option(help="Comment body text.")] = "",
        body_file: Annotated[str, typer.Option(help="Path to a file containing the comment body.")] = "",
    ) -> CommentResult:
        """Post a comment to an issue or work item by its URL.

        Resolves the code host per-URL across all registered overlays, so it
        works for any tracker an overlay is configured for (GitLab issues and
        work items, GitHub issues). Pass the body inline with ``--body`` or
        from a file with ``--body-file``.
        """
        from pathlib import Path  # noqa: PLC0415

        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

        text = Path(body_file).read_text(encoding="utf-8") if body_file else body
        if not text:
            return {"error": "No comment body: pass --body or --body-file"}

        for overlay in get_all_overlays().values():
            host = get_code_host_for_url(overlay, issue_url)
            if host is None:
                continue
            raw = host.post_issue_comment(issue_url=issue_url, body=text)
            error = raw.get("error") if isinstance(raw, dict) else None
            if error:
                self.stdout.write(f"  failed: {error}")
                return {"error": str(error)}
            comment_id = raw.get("id") if isinstance(raw, dict) else None
            self.stdout.write(f"  commented on {issue_url}")
            return {
                "issue_url": issue_url,
                "comment_id": comment_id if isinstance(comment_id, int) else 0,
            }

        return {"error": f"No code host could be resolved for {issue_url}"}

    @command(name="create-sub")
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def create_sub(  # noqa: PLR0913 — django-typer command: every param is a CLI flag mapped 1:1 to the public --parent/--title/--description/--description-file/--labels/--type surface (same rationale as `clear`), not an internal design smell.
        self,
        *,
        parent: Annotated[str, typer.Option(help="Parent issue/work-item URL the child is nested under.")] = "",
        title: Annotated[str, typer.Option(help="Title of the child work item.")] = "",
        description: Annotated[str, typer.Option(help="Child description text.")] = "",
        description_file: Annotated[str, typer.Option(help="Path to a file containing the child description.")] = "",
        labels: Annotated[str, typer.Option(help="Comma-separated labels for the child.")] = "",
        type: Annotated[str, typer.Option(help="Child work-item type: Task (default), Incident, or Issue.")] = "Task",  # noqa: A002 — the public CLI flag is ``--type``; shadowing the builtin here is the option name, not a usage.
    ) -> CreateSubResult:
        """Create a child work item nested under a parent issue/work item.

        Resolves the code host per-URL across all registered overlays (the
        same resolver ``comment`` uses). On GitLab the child is created, then
        converted to ``--type`` and linked under ``--parent`` as one operation
        — an Issue→Issue parent link is forbidden, so the default ``Task`` is
        the natural sub-item. Pass the description inline with ``--description``
        or from a file with ``--description-file``. Prints the child IID and URL
        for chaining into dispatch prompts.
        """
        from pathlib import Path  # noqa: PLC0415

        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

        if not parent.strip() or not title.strip():
            return {"error": "create-sub refused: --parent and --title are both required"}

        body = Path(description_file).read_text(encoding="utf-8") if description_file else description
        label_list = [label.strip() for label in labels.split(",") if label.strip()]

        for overlay in get_all_overlays().values():
            host = get_code_host_for_url(overlay, parent)
            if host is None:
                continue
            raw = host.create_sub_issue(
                parent_url=parent,
                title=title,
                body=body,
                labels=label_list,
                child_type=type,
            )
            error = raw.get("error") if isinstance(raw, dict) else None
            if error:
                self.stdout.write(f"  failed: {error}")
                return {"error": str(error)}
            child_iid = raw.get("iid") if isinstance(raw, dict) else None
            child_url = raw.get("web_url") if isinstance(raw, dict) else None
            child_url = str(child_url) if isinstance(child_url, str) else ""
            self.stdout.write(f"  created #{child_iid} {child_url}")
            return {
                "parent_url": parent,
                "child_iid": child_iid if isinstance(child_iid, int) else 0,
                "child_url": child_url,
            }

        return {"error": f"No code host could be resolved for {parent}"}

    @command(name="list")
    def list_tickets(self, state: str = "", overlay: str = "") -> list[dict[str, object]]:
        """List tickets, optionally filtered by state and/or overlay."""
        qs = Ticket.objects.order_by("-pk")
        if state:
            qs = qs.filter(state=state)
        if overlay:
            qs = qs.filter(overlay=overlay)
        return [
            {
                "id": int(ticket.pk),
                "state": ticket.state,
                "overlay": ticket.overlay,
                "issue_url": ticket.issue_url,
                "variant": ticket.variant,
            }
            for ticket in qs
        ]

    @command()
    def sync_completions(
        self,
        *,
        dry_run: Annotated[bool, typer.Option(help="Show what would transition without acting.")] = False,
    ) -> list[CompletionResult]:
        """Check post-ship tickets against upstream issues and advance completed ones.

        Walks tickets in shipped/in_review/merged states, calls the overlay's
        ``is_issue_done()`` for each, and transitions completed tickets toward
        delivered. Use ``--dry-run`` to preview without touching state.
        """
        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

        completable_states = frozenset({"shipped", "in_review", "merged"})
        results: list[CompletionResult] = []

        for overlay_name, overlay in get_all_overlays().items():
            tickets = Ticket.objects.filter(
                state__in=completable_states,
                overlay=overlay_name,
            ).exclude(issue_url="")

            for ticket in tickets:
                host = get_code_host_for_url(overlay, ticket.issue_url)
                if host is None:
                    continue
                try:
                    issue_data = host.get_issue(ticket.issue_url)
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to fetch issue for ticket %s (%s)", ticket.pk, ticket.issue_url)
                    continue
                if not isinstance(issue_data, dict) or "error" in issue_data:
                    continue
                if not overlay.is_issue_done(issue_data):
                    continue

                from_state = ticket.state
                if dry_run:
                    results.append(
                        CompletionResult(
                            ticket_id=int(ticket.pk),
                            issue_url=ticket.issue_url,
                            from_state=from_state,
                            action="would_complete",
                        )
                    )
                    self.stdout.write(f"  [dry-run] #{ticket.pk} ({from_state}) → completed: {ticket.issue_url}")
                else:
                    _advance_ticket(ticket)
                    results.append(
                        CompletionResult(
                            ticket_id=int(ticket.pk),
                            issue_url=ticket.issue_url,
                            from_state=from_state,
                            to_state=ticket.state,
                            action="completed",
                        )
                    )
                    self.stdout.write(f"  #{ticket.pk} {from_state} → {ticket.state}: {ticket.issue_url}")

        if not results:
            self.stdout.write("No tickets to advance.")
        else:
            self.stdout.write(f"\n{len(results)} ticket(s) {'would be' if dry_run else ''} advanced.")
        return results

    @command()
    def reconcile_overlay(
        self,
        *,
        dry_run: Annotated[bool, typer.Option(help="Show what would change without persisting.")] = False,
    ) -> list[ReattributeResult]:
        """Backfill ``overlay`` for rows whose attribution disagrees with inference.

        Walks every ticket with an ``issue_url`` and re-runs overlay
        inference (now routed through ``get_workspace_repos()``). Rows whose
        stored overlay differs from a *conclusive* inference are corrected;
        an inconclusive (empty) inference never blanks an existing value.
        Use ``--dry-run`` to preview.
        """
        results: list[ReattributeResult] = []

        for ticket in Ticket.objects.exclude(issue_url="").order_by("pk"):
            inferred = ticket._infer_overlay()  # noqa: SLF001 — backfill owns this model concern.
            if not inferred or inferred == ticket.overlay:
                continue

            from_overlay = ticket.overlay
            from_label = from_overlay or "(none)"
            if dry_run:
                results.append(
                    ReattributeResult(
                        ticket_id=int(ticket.pk),
                        issue_url=ticket.issue_url,
                        from_overlay=from_overlay,
                        to_overlay=inferred,
                        action="would_reattribute",
                    )
                )
                self.stdout.write(f"  [dry-run] #{ticket.pk}: {from_label} → {inferred}: {ticket.issue_url}")
            else:
                ticket.apply_inferred_overlay(inferred)
                results.append(
                    ReattributeResult(
                        ticket_id=int(ticket.pk),
                        issue_url=ticket.issue_url,
                        from_overlay=from_overlay,
                        to_overlay=ticket.overlay,
                        action="reattributed",
                    )
                )
                self.stdout.write(f"  #{ticket.pk}: {from_label} → {ticket.overlay}: {ticket.issue_url}")

        if not results:
            self.stdout.write("All ticket overlays already consistent with inference.")
        else:
            verb = "would be" if dry_run else "were"
            self.stdout.write(f"\n{len(results)} ticket(s) {verb} re-attributed.")
        return results


def _advance_ticket(ticket: Ticket) -> None:
    """Walk the ticket through remaining FSM transitions toward delivered."""
    with transaction.atomic():
        if ticket.state == "shipped":
            ticket.request_review()
            ticket.save()
    with transaction.atomic():
        if ticket.state == "in_review":
            ticket.mark_merged()
            ticket.save()
    with transaction.atomic():
        if ticket.state == "merged":
            ticket.retrospect()
            ticket.save()

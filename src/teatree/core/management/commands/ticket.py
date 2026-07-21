"""Ticket state management: transitions and listing for the loop and CLI."""

from typing import Annotated, TypedDict

import typer
from django.db import transaction
from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command

from teatree.core.gates.schema_guard import SelfDbMigrationError, require_current_schema
from teatree.core.management.commands._attachment_commands import AttachmentCommands
from teatree.core.management.commands._clear_preflight import clear_preflight_refusal
from teatree.core.management.commands._close_commands import CloseCommands
from teatree.core.management.commands._context_commands import ContextCommands
from teatree.core.management.commands._merge_keystone_commands import MergeKeystoneCommands
from teatree.core.management.commands._plan_commands import PlanCommands
from teatree.core.management.commands._rubric_commands import RubricCommands
from teatree.core.management.commands._sweep_commands import SweepCommands
from teatree.core.management.commands._ticket_show import TicketShowCommands
from teatree.core.management.commands._transition_names import ALLOWED_TRANSITIONS
from teatree.core.management.commands._transition_refusals import review_context_refusal
from teatree.core.merge import MergePreconditionError, resolve_pr_repo_slug
from teatree.core.models import ClearIssuanceError, ClearRequest, MergeClear, ReviewVerdict, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.external_delivery import refresh_external_delivery_if_active
from teatree.core.send_proxy import forge_from_url, route_forge_write


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


# The 8-mixin base list is a django-typer requirement, not a composition-bar
# violation: django-typer discovers ``@command``-decorated methods by walking the
# Command class's own MRO, so each cohesive command group (rubric, plan, show,
# context, close, attachment, merge-keystone, sweep) MUST be a base class of the
# single ``Command`` rather than a plain collaborator it delegates to — a helper
# object's methods would never register as CLI subcommands. The mixins stay
# single-concern; only their registration is inheritance-shaped.
class Command(
    RubricCommands,
    PlanCommands,
    TicketShowCommands,
    ContextCommands,
    CloseCommands,
    AttachmentCommands,
    MergeKeystoneCommands,
    SweepCommands,
    TyperCommand,
):
    @command()
    def transition(self, ticket_id: int, transition_name: str) -> dict[str, object]:
        """Transition a ticket to a new state.

        Accepts any of the allowed transition names: scope, start, code, test,
        review, ship, request_review, mark_merged, retrospect, mark_delivered,
        rework, mark_review_no_action.
        """
        if transition_name not in ALLOWED_TRANSITIONS:
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
        from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

        from teatree.core.models.types import DodE2EOverride  # noqa: PLC0415 — deferred: ORM/app-registry

        cleaned = reason.strip()
        if not cleaned:
            self.stderr.write("  refused: --reason is required (a silent DoD-gate bypass is not allowed).")
            raise SystemExit(1)
        ticket = self._resolve_ticket(ticket_id)
        recorded_at = timezone.now().isoformat()
        ticket.merge_extra(set_keys={"dod_e2e_override": DodE2EOverride(reason=cleaned, by=by.strip(), at=recorded_at)})
        self.stdout.write(f"  DoD local-E2E gate override recorded for ticket {ticket.pk}")
        return DodOverrideResult(ticket_id=int(ticket.pk), reason=cleaned, by=by.strip(), at=recorded_at)

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
        from teatree.core.models.e2e_bypass import E2EBypassApproval, E2EBypassApprovalError  # noqa: PLC0415 — lazy ORM

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

    def _resolve_ticket(self, ticket_id: int) -> Ticket:
        """Fetch a ticket or abort the subcommand with a nonzero exit (#932).

        A missing ticket is a real failure — returning an ``{"error": …}`` dict
        would print and exit 0, so a scripted caller could not tell success from
        "ticket not found". ``raise SystemExit(1)`` is the sibling refusal convention.
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
        expedite_authorize: Annotated[
            str,
            typer.Option(
                "--expedite-authorize",
                help=(
                    "PENDING-checks waiver: the human/owner id authorising a merge on queued "
                    "(never FAILED) required checks. Requires a ticket flagged expedited AND "
                    "--local-ci-green-sha bound to the reviewed tree."
                ),
            ),
        ] = "",
        local_ci_green_sha: Annotated[
            str,
            typer.Option(
                "--local-ci-green-sha",
                help=(
                    "Attestation that the local full CI lane (dev/test-cov.sh + ruff, tree-wide "
                    "gates) ran green at exactly this reviewed SHA — must equal --reviewed-sha."
                ),
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
            expedite_authorizer=expedite_authorize,
            local_ci_green_sha=local_ci_green_sha,
            executing_loop_identity=executing_loop_identity,
        )

        # Resolve the verdict's owner/repo BEFORE issuing: resolve_pr_repo_slug
        # fails closed in a degenerate environment (workstream slug, no ticket
        # issue_url, no clone origin), and resolving it after MergeClear.issue()
        # persisted the row would orphan an already-issued CLEAR behind a traceback.
        # Issue runs only when resolution succeeds, so neither failure persists a row.
        #
        # Issue + record the sibling verdict inside ONE transaction (F3.2): the
        # CLEAR and its read-side ReviewVerdict are two halves of one atomic
        # keystone step. If ``ReviewVerdict.record`` raises AFTER ``issue()``
        # persisted the row, the outer atomic rolls the CLEAR back too — so a
        # verdict failure can never leave an issued CLEAR with no matching verdict
        # (a phantom that `review status` would later read as merge-safe).
        try:
            verdict_slug = resolve_pr_repo_slug(request)
            with transaction.atomic():
                clear = MergeClear.issue(request)
                # Record the durable read-side sibling (a merge-safe judgment by
                # construction — issuance refused any non-green verdict) so a later
                # `review status` answers "safe to approve at the current head?".
                # Key it under verdict_slug — where the merge gate queries — not
                # the workstream slug.
                verdict = ReviewVerdict.record(
                    pr_id=clear.pr_id,
                    slug=verdict_slug,
                    reviewed_sha=clear.reviewed_sha,
                    verdict=ReviewVerdict.Verdict.MERGE_SAFE,
                    reviewer_identity=clear.reviewer_identity,
                    blast_class=clear.blast_class,
                    gh_verify_result=clear.gh_verify_result,
                    ticket=resolved_ticket,
                    # A pending expedite CLEAR records the sibling merge_safe verdict
                    # on PENDING checks; the flag lets ``record`` accept it
                    # (§17.4.3 / PR-07).
                    expedited=bool(clear.expedite_authorizer),
                )
        except (MergePreconditionError, ClearIssuanceError) as exc:
            self.stdout.write(f"  CLEAR refused: {exc}")
            return {"issued": False, "error": str(exc)}

        self.stdout.write(f"  issued CLEAR {clear.pk} for {clear.slug}#{clear.pr_id}@{clear.reviewed_sha[:8]}")
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
        from pathlib import Path  # noqa: PLC0415 — deferred: loaded only when this command runs

        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415 — deferred: lazy command import
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: keeps command import light

        text = Path(body_file).read_text(encoding="utf-8") if body_file else body
        if not text:
            return {"error": "No comment body: pass --body or --body-file"}
        # The shared forge-write seam (public-repo leak gate + #117 send-proxy) — same seam the MCP tools use.
        forge = forge_from_url(issue_url)
        text = route_forge_write(forge=forge, repo=issue_url, text=text, action="ticket_comment", target=issue_url)
        for overlay in get_all_overlays().values():
            host = get_code_host_for_url(overlay, issue_url)
            if host is None:
                continue
            raw = host.post_issue_comment(issue_url=issue_url, body=text)
            if isinstance(raw, dict) and raw.get("error"):
                self.stdout.write(f"  failed: {raw['error']}")
                return {"error": str(raw["error"])}
            comment_id = raw.get("id") if isinstance(raw, dict) else None
            self.stdout.write(f"  commented on {issue_url}")
            return {"issue_url": issue_url, "comment_id": comment_id if isinstance(comment_id, int) else 0}
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
        from pathlib import Path  # noqa: PLC0415 — deferred: loaded only when this command runs

        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415 — deferred: lazy command import
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: keeps command import light

        if not parent.strip() or not title.strip():
            return {"error": "create-sub refused: --parent and --title are both required"}

        body = Path(description_file).read_text(encoding="utf-8") if description_file else description
        label_list = [label.strip() for label in labels.split(",") if label.strip()]

        # Route the child title/body/labels through the shared forge-write seam
        # (public-repo leak gate + #117 send-proxy) — the SAME seam the sibling
        # `comment` and the MCP issue_create twin use — so a child carrying a
        # customer codename bound for a public forge is REFUSED before the create.
        # Labels ride the scrub too (a forge auto-creates a missing label),
        # matching the MCP twin.
        forge = forge_from_url(parent)
        title = route_forge_write(forge=forge, repo=parent, text=title, action="ticket_create_sub", target=parent)
        body = route_forge_write(forge=forge, repo=parent, text=body, action="ticket_create_sub", target=parent)
        label_list = [
            route_forge_write(forge=forge, repo=parent, text=label, action="ticket_create_sub", target=parent)
            for label in label_list
        ]

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

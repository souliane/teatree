"""Ticket state management: transitions and listing for the loop and CLI."""

import logging
from typing import Annotated, TypedDict

import click
import typer
from django.db import transaction
from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command, group

from teatree.core.management.commands._clear_branch_currency import check_clear_branch_currency
from teatree.core.merge_execution import MergePreconditionError, merge_ticket_pr
from teatree.core.models import ClearIssuanceError, ClearRequest, MergeClear, Ticket
from teatree.core.schema_guard import SelfDbMigrationError, require_current_schema


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


class MergeKeystoneResult(TypedDict, total=False):
    merged: bool
    pr_id: int
    slug: str
    merged_sha: str
    ticket_id: int
    ticket_state: str
    error: str
    escalated: bool


class ClearIssueResult(TypedDict, total=False):
    issued: bool
    clear_id: int
    pr_id: int
    slug: str
    blast_class: str
    human_authorizer: str
    ticket_id: int
    error: str


class ContextResult(TypedDict, total=False):
    ticket_id: int
    context: str


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
}


class Command(TyperCommand):
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
        except TransitionNotAllowed:
            return {
                "error": f"Transition '{transition_name}' not allowed from state '{ticket.state}'",
            }

        return {"ticket_id": int(ticket.pk), "state": ticket.state}

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

    @group(help="Durable per-ticket knowledge store (#627).")
    def context(self) -> None:
        """Group root — forces sub-commands to be addressed by name."""

    @context.command(name="show")
    def context_show(self, ticket_id: int) -> ContextResult:
        """Print the ticket's durable context store."""
        ticket = self._resolve_ticket(ticket_id)
        self.stdout.write(ticket.context or "(empty)")
        return {"ticket_id": int(ticket.pk), "context": ticket.context}

    @context.command(name="add")
    def context_add(self, ticket_id: int, entry: str) -> ContextResult:
        """Append a timestamped ``<key>: <value>`` line to the context store.

        Append-only: parallel sessions never overwrite each other (open
        question 2). A blank entry is refused with a nonzero exit.
        """
        ticket = self._resolve_ticket(ticket_id)
        try:
            updated = ticket.append_context(entry)
        except ValueError as exc:
            self.stderr.write(f"  refused: {exc}")
            raise SystemExit(1) from exc
        self.stdout.write(f"  appended to ticket {ticket.pk} context")
        return {"ticket_id": int(ticket.pk), "context": updated}

    @context.command(name="edit")
    def context_edit(self, ticket_id: int) -> ContextResult:
        """Open the full context store in ``$EDITOR`` and replace it.

        Unlike ``add``, ``edit`` is a full-field rewrite — for pruning stale
        entries or restructuring. An aborted edit (editor exits without
        saving) leaves the store untouched.
        """
        ticket = self._resolve_ticket(ticket_id)
        edited = click.edit(ticket.context)
        if edited is None:
            self.stdout.write(f"  edit aborted — ticket {ticket.pk} context unchanged")
            return {"ticket_id": int(ticket.pk), "context": ticket.context}
        ticket.context = edited
        ticket.save(update_fields=["context"])
        self.stdout.write(f"  ticket {ticket.pk} context replaced")
        return {"ticket_id": int(ticket.pk), "context": edited}

    @command()
    def clear(  # noqa: PLR0913 — django-typer command: every param is a CLI flag mapped 1:1 to a §17.4.2 CLEAR field; the arg list IS the public CLI surface (same rationale as the file-wide PLR6301 ignore), not an internal design smell.
        self,
        pr_id: int,
        slug: str,
        reviewed_sha: str,
        *,
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

        # #940 branch-currency pre-flight: refuse a CLEAR whose
        # ``reviewed_sha`` trails the target branch. Otherwise the cold
        # reviewer attests a tree missing target-branch fixes and the
        # release pipeline certifies a stale base. Run BEFORE
        # ``MergeClear.issue`` so the CLEAR — if issued — already points
        # at a current SHA.
        currency_error = check_clear_branch_currency(reviewed_sha, resolved_ticket)
        if currency_error is not None:
            self.stdout.write(f"  CLEAR refused: {currency_error}")
            return {"issued": False, "error": currency_error}

        try:
            clear = MergeClear.issue(
                ClearRequest(
                    pr_id=pr_id,
                    slug=slug,
                    reviewed_sha=reviewed_sha,
                    reviewer_identity=reviewer_identity,
                    gh_verify_result=gh_verify_result,
                    blast_class=blast_class,
                    ticket=resolved_ticket,
                    human_authorizer=human_authorize,
                    executing_loop_identity=executing_loop_identity,
                )
            )
        except ClearIssuanceError as exc:
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

        try:
            outcome = merge_ticket_pr(
                clear=clear,
                executing_loop_identity=loop_identity,
                human_authorized=human_authorized,
            )
        except MergePreconditionError as exc:
            self.stdout.write(f"  merge refused (re-escalating): {exc}")
            return {
                "merged": False,
                "escalated": True,
                "pr_id": int(clear.pr_id),
                "slug": clear.slug,
                "error": str(exc),
            }

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

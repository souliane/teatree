"""The ``ticket`` plan-gate operator commands, factored out of ``ticket.py``.

Covers ``plan`` / ``plan-bypass`` / ``skip-planning`` / ``plan-reconcile-inflight``
/ ``plan-reaffirm``. They live here as a :class:`PlanCommands` mixin that the ``ticket``
:class:`~django_typer.management.TyperCommand` inherits from, so they mount under
``t3 <overlay> ticket plan`` (etc.) while their LOC stays out of the (cap-bound)
``ticket.py`` god-module — the same split as :class:`RubricCommands`. django-typer
collects ``@command`` methods from every ``TyperCommand`` base in the MRO, so the
CLI surface is unchanged. The pure validate/record/advance logic lives in
``_plan_gate_commands``; these methods are the thin CLI shells (flag parsing +
``stderr`` refusals + structured results).
"""

import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.management.commands._plan_gate_commands import (
    PlanAdvanceError,
    PlanReconcileResult,
    PlanResult,
    ReaffirmError,
    reaffirm_plan,
    reconcile_inflight,
    record_artifact_and_advance,
    record_bypass_and_advance,
    record_trivial_skip_and_advance,
)
from teatree.core.models import Ticket
from teatree.core.models.external_delivery import refresh_external_delivery_if_active


class PlanCommands(TyperCommand):
    """The plan-gate operator command surface (mixed into the ``ticket`` command)."""

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
        base_sha: Annotated[
            str,
            typer.Option(
                "--base-sha",
                help="Target-branch HEAD (40-char hex) the plan was authored against. "
                "Required under require_plan_adequacy.",
            ),
        ] = "",
        adequacy_json: Annotated[
            str,
            typer.Option(
                "--adequacy-json",
                help="Four-section adequacy manifest as a JSON object "
                "(design/integration_seams/edge_cases/test_strategy). Required under require_plan_adequacy.",
            ),
        ] = "",
    ) -> PlanResult:
        """Record a PlanArtifact and advance the ticket STARTED → PLANNED.

        The operator-facing plan recorder named by the ``NoPlanArtifactError``
        message: a planning task that finished out-of-band, or a ticket the
        planner never ran on, advances by recording the plan here. A blank
        ``plan_text`` is refused — a vacuous artifact cannot advance the FSM. Under
        ``require_plan_adequacy`` ``--base-sha`` + ``--adequacy-json`` are also
        required (a thin spec is refused). For an *audited bypass* (no real plan,
        explicit human sign-off) use ``plan-bypass``; for a trivial mechanical edit
        use ``skip-planning``.
        """
        cleaned_text = plan_text.strip()
        if not cleaned_text:
            self.stderr.write("  refused: plan_text is required (a vacuous plan cannot advance the FSM)")
            raise SystemExit(1)

        adequacy = self._parse_adequacy_json(adequacy_json)
        ticket = self._resolve_plan_ticket(ticket_id)
        try:
            artifact = record_artifact_and_advance(
                ticket=ticket,
                plan_text=cleaned_text,
                recorded_by=recorded_by.strip() or "operator",
                base_sha=base_sha.strip(),
                adequacy=adequacy,
            )
        except PlanAdvanceError as exc:
            return PlanResult(ticket_id=int(ticket.pk), error=exc.message)

        # #2217: external-owner FSM seam — refresh a LIVE lease (no-op without one).
        refresh_external_delivery_if_active(ticket)
        self.stdout.write(f"  plan recorded for ticket {ticket.pk} (artifact {artifact.pk}); state → {ticket.state}")
        return PlanResult(ticket_id=int(ticket.pk), artifact_id=int(artifact.pk), state=ticket.state)

    @command(name="plan-bypass")
    def plan_bypass(
        self,
        ticket_id: int,
        *,
        human_authorize: Annotated[
            str,
            typer.Option("--human-authorize", help="Username of the human explicitly authorising this plan bypass."),
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

        ticket = self._resolve_plan_ticket(ticket_id)
        try:
            artifact = record_bypass_and_advance(
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

        ticket = self._resolve_plan_ticket(ticket_id)
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

    @command(name="plan-reaffirm")
    def plan_reaffirm(
        self,
        ticket_id: int,
        *,
        base_sha: Annotated[
            str,
            typer.Option("--base-sha", help="The NEW target-branch HEAD (40-char hex) to re-bind the plan to."),
        ],
        disposition: Annotated[
            list[str],
            typer.Option(
                "--disposition",
                help="How an intervening seam-touching commit affects the plan. Repeat one per such commit.",
            ),
        ] = [],  # noqa: B006 — django-typer resolves a list Option default per-invocation.
        by: Annotated[str, typer.Option(help="Who is reaffirming (audit trail).")] = "operator",
    ) -> PlanResult:
        """Re-bind a stale plan to the new base after dispositioning intervening seam changes.

        The remediation the plan-currency gate (SELFCATCH-3) names when a plan goes
        stale: appends a NEW PlanArtifact at ``--base-sha`` carrying the prior plan's
        adequacy forward, but REFUSES unless a ``--disposition`` is supplied for each
        intervening commit that touched a declared integration seam. This is the
        never-lockout escape — a stale plan is never a hard trap, only a demand to
        reckon with what moved.
        """
        ticket = self._resolve_plan_ticket(ticket_id)
        try:
            artifact = reaffirm_plan(
                ticket=ticket, new_base_sha=base_sha, dispositions=disposition, by=by.strip() or "operator"
            )
        except ReaffirmError as exc:
            self.stderr.write(f"  plan-reaffirm refused: {exc.message}")
            return PlanResult(ticket_id=int(ticket.pk), error=exc.message)

        self.stdout.write(
            f"  plan reaffirmed for ticket {ticket.pk} (artifact {artifact.pk}) at base {artifact.base_sha[:12]}"
        )
        return PlanResult(ticket_id=int(ticket.pk), artifact_id=int(artifact.pk), state=ticket.state)

    @staticmethod
    def _parse_adequacy_json(adequacy_json: str) -> dict | None:
        """Parse the ``--adequacy-json`` option into a manifest dict, or ``None`` when blank."""
        cleaned = adequacy_json.strip()
        if not cleaned:
            return None
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            msg = f"--adequacy-json is not valid JSON: {exc}"
            raise typer.BadParameter(msg) from exc
        if not isinstance(parsed, dict):
            msg = "--adequacy-json must be a JSON object (the four-section manifest)"
            raise typer.BadParameter(msg)
        return parsed

    def _resolve_plan_ticket(self, ticket_id: int) -> Ticket:
        """Fetch a ticket or abort the subcommand with a nonzero exit (mixin-local resolver)."""
        try:
            return Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  Ticket {ticket_id} not found")
            raise SystemExit(1) from None

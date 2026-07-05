"""The ``ticket merge`` keystone command, factored out of ``ticket.py``.

The sole sanctioned ``IN_REVIEW`` → ``MERGED`` transition (§17.4) lives here as a
:class:`MergeKeystoneCommands` mixin the ``ticket``
:class:`~django_typer.management.TyperCommand` inherits from, so ``t3 <overlay>
ticket merge`` mounts unchanged while its LOC stays out of the (cap-bound)
``ticket.py`` god-module. django-typer collects ``@command`` methods from every
``TyperCommand`` base in the MRO, so the mixin is the idiomatic split. It carries
the human-authorized substrate (``--human-authorized``) and PENDING-checks
expedite (``--expedite-authorized``) approval surfaces (§17.4.3 / PR-07).
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.gates.owned_repo_guard import MergeKeystoneResult, escalated_merge_result, merge_clear_refusal
from teatree.core.gates.schema_guard import SelfDbMigrationError, require_current_schema
from teatree.core.merge import MergePreconditionError, merge_ticket_pr
from teatree.core.models import MergeClear


class MergeKeystoneCommands(TyperCommand):
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
        expedite_authorized: Annotated[
            str,
            typer.Option(
                "--expedite-authorized",
                help=(
                    "Expedite-only: the recorded expedite authoriser id, re-presented to waive a "
                    "PENDING (never FAILED) required check on an expedite CLEAR. Distinct from "
                    "--human-authorized so the substrate hold and the pending waiver never cross-unlock."
                ),
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

        if (
            scope_refusal := merge_clear_refusal(clear, approved=bool(human_authorized or expedite_authorized))
        ) is not None:
            return scope_refusal

        try:
            outcome = merge_ticket_pr(
                clear=clear,
                executing_loop_identity=loop_identity,
                human_authorized=human_authorized,
                expedite_authorized=expedite_authorized,
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

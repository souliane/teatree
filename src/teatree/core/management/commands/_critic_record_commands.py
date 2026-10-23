"""Record a hand critic through the same guarded path as a returned worker verdict."""

import json
from pathlib import Path
from typing import IO, Annotated, NotRequired, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.gates.critic_gate import record_returned_critic_verdict
from teatree.core.machine_output import emit
from teatree.core.models.critic_dispatch import CriticDispatch
from teatree.core.models.critic_verdict import CriticVerdict


class CriticRecordReceipt(TypedDict):
    recorded: bool
    error: NotRequired[str]
    existing: NotRequired[bool]
    verdict_id: NotRequired[int]
    head_sha: NotRequired[str]


class CriticRecordCommands(TyperCommand):
    @command()
    def critic_record(
        self,
        ticket_id: int,
        head_sha: str,
        envelope_file: Path,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the receipt as JSON.")] = False,
    ) -> CriticRecordReceipt:
        """Record a merge critic envelope for the ticket's dispatched head."""
        self.print_result = False
        result = self._record_critic(ticket_id, head_sha, envelope_file)
        emit(
            result,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=str(result),
        )
        return result

    def _record_critic(self, ticket_id: int, head_sha: str, envelope_file: Path) -> CriticRecordReceipt:
        try:
            result = json.loads(envelope_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"recorded": False, "error": str(exc)}
        if not isinstance(result, dict) or not isinstance(result.get("critic_verdict"), dict):
            return {"recorded": False, "error": "file must contain a critic_verdict object"}
        dispatch = (
            CriticDispatch.objects.filter(ticket_id=ticket_id, transition="merge", head_sha=head_sha.lower())
            .select_related("task")
            .first()
        )
        if dispatch is None or dispatch.task is None:
            return {"recorded": False, "error": "no merge critic dispatch for this ticket and head"}
        existing = CriticVerdict.objects.latest_for(ticket=dispatch.ticket, transition="merge", head_sha=head_sha)
        if existing is not None:
            error = "a verdict already covers this head; the supplied envelope was not recorded"
            return {
                "recorded": False,
                "existing": True,
                "verdict_id": existing.pk,
                "head_sha": existing.head_sha,
                "error": error,
            }
        if error := record_returned_critic_verdict(dispatch.task, result):
            return {"recorded": False, "error": error}
        verdict = CriticVerdict.objects.latest_for(ticket=dispatch.ticket, transition="merge", head_sha=head_sha)
        return (
            {"recorded": False, "error": "critic verdict was not persisted"}
            if verdict is None
            else {"recorded": True, "verdict_id": verdict.pk, "head_sha": verdict.head_sha}
        )

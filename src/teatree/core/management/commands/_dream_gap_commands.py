"""``t3 dream gap-coverage`` / ``gap-disposition`` — the dream-gap ledger read and per-gap record.

Factored out of ``dream.py`` as a :class:`DreamGapCommands` mixin, the ``CloseCommands``
shape: django-typer collects ``@command`` methods from every ``TyperCommand`` base in the MRO.
"""

from dataclasses import asdict
from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.models import Ticket
from teatree.core.models.dream_gap_ledger import DreamGapLedgerError, dream_umbrella_url, record_gap_disposition
from teatree.loops.dream.gap_coverage import GapCoverageReport, gap_coverage


def _render(report: GapCoverageReport) -> str:
    lines = [f"  {report.gaps} dream gap(s) — {'every one owned exactly once' if report.ok else 'NOT fully covered'}"]
    lines.extend(f"  orphan: {key}" for key in report.orphan)
    lines.extend(f"  duplicate: {key} owned by {pks}" for key, pks in report.duplicate.items())
    lines.extend(f"  retired ticket still owns gaps: {pk}" for pk in report.retired_owner)
    lines.extend(f"  stranded memory row (no owner re-offers it): {key}" for key in report.stranded)
    lines.extend(f"  undispositioned: {key}" for key in report.undispositioned)
    return "\n".join(lines)


class DreamGapCommands(TyperCommand):
    @command(name="gap-coverage")
    def gap_coverage_report(
        self,
        *,
        ticket: Annotated[
            int, typer.Option("--ticket", help="Also require every gap folded into this host to carry a disposition.")
        ] = 0,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the report as JSON.")] = False,
    ) -> None:
        """Prove every dream gap has exactly one owner; exit 1 on an orphan, duplicate, bad link or open gap."""
        host = Ticket.objects.filter(pk=ticket).first() if ticket else None
        if ticket and host is None:
            self.stderr.write(f"  gap-coverage refused: no ticket {ticket}")
            raise SystemExit(1)
        report = gap_coverage(umbrella_url=dream_umbrella_url(), host=host)
        self.print_result = False
        emit(
            {**asdict(report), "ok": report.ok},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_render(report),
        )
        if not report.ok:
            raise SystemExit(1)

    @command(name="gap-disposition")
    def gap_disposition(
        self,
        ticket: Annotated[int, typer.Argument(help="The host ticket the gap is folded into.")],
        gap_key: Annotated[str, typer.Argument(help="The folded gap's key.")],
        *,
        citation: Annotated[str, typer.Option("--citation", help="ADDRESS: the evidence that verifies the gap.")] = "",
        reject: Annotated[str, typer.Option("--reject", help="REJECT: why the gap is not worth addressing.")] = "",
    ) -> None:
        """Record a folded dream gap's ADDRESS (with its citation) or reasoned REJECT on its host."""
        host = Ticket.objects.filter(pk=ticket).first()
        if host is None:
            self.stderr.write(f"  gap-disposition refused: no ticket {ticket}")
            raise SystemExit(1)
        try:
            recorded = record_gap_disposition(host, gap_key, citation=citation, rejection=reject)
        except DreamGapLedgerError as exc:
            self.stderr.write(f"  gap-disposition refused: {exc}")
            raise SystemExit(1) from exc
        self.stdout.write(f"  {gap_key}: {recorded} recorded on ticket {host.pk}")

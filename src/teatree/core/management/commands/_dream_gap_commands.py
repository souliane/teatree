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
from teatree.core.models.dream_gap_types import DreamFunnelEntry
from teatree.loops.dream.funnel import QuestionClassStatus, daily_funnel, question_class_status, question_classes
from teatree.loops.dream.gap_coverage import GapCoverageReport, gap_coverage


def _render(report: GapCoverageReport) -> str:
    lines = [f"  {report.gaps} dream gap(s) — {'every one owned exactly once' if report.ok else 'NOT fully covered'}"]
    lines.extend(f"  orphan: {key}" for key in report.orphan)
    lines.extend(f"  duplicate: {key} owned by {pks}" for key, pks in report.duplicate.items())
    lines.extend(f"  retired ticket still owns gaps: {pk}" for pk in report.retired_owner)
    lines.extend(f"  stranded memory row (no owner re-offers it): {key}" for key in report.stranded)
    lines.extend(f"  undispositioned: {key}" for key in report.undispositioned)
    return "\n".join(lines)


def _render_funnel(daily: list[DreamFunnelEntry], classes: list[QuestionClassStatus]) -> str:
    lines = [
        f"  {row['at'][:10]} rows {row.get('rows', 0)}, kept {row.get('kept', 0)}, "
        f"core {row.get('core_gaps', 0)}, pending {row.get('pending', 0)}, folded {row.get('folded', 0)}, "
        f"addressed {row.get('addressed', 0)}, rejected {row.get('rejected', 0)}, "
        f"retired {row.get('retired', 0)}, stranded {row.get('stranded_hosts', 0)}, "
        f"orphans {row.get('orphans', 0)}, withheld {row.get('withheld', 0)}"
        for row in daily
    ]
    lines.extend(f"  class {status.name}: {'fixed' if status.fixed else 'open'}" for status in classes)
    return "\n".join(lines) or "  no funnel samples"


class DreamGapCommands(TyperCommand):
    @command(name="funnel")
    def funnel_report(
        self,
        *,
        question_class_name: Annotated[str, typer.Option("--class", help="Inspect one recurring question class.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit the daily funnel as JSON.")] = False,
    ) -> None:
        daily = daily_funnel(dream_umbrella_url())
        if question_class_name:
            status = question_class_status(question_class_name)
            if status is None:
                self.stderr.write(f"  unknown question class: {question_class_name}")
                raise SystemExit(1)
            classes = [status]
        else:
            classes = question_classes()
        payload = {
            "daily": daily,
            "classes": [
                {
                    "name": status.name,
                    "fixed": status.fixed,
                    "daily_counts": status.daily_counts,
                    "last_generated_at": status.last_generated_at.isoformat(),
                }
                for status in classes
            ],
        }
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_render_funnel(daily, classes),
        )

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
        misfit: Annotated[str, typer.Option("--misfit", help="RETURN: why this host cannot address the gap.")] = "",
    ) -> None:
        """Record a folded dream gap's ADDRESS (with its citation) or reasoned REJECT on its host."""
        host = Ticket.objects.filter(pk=ticket).first()
        if host is None:
            self.stderr.write(f"  gap-disposition refused: no ticket {ticket}")
            raise SystemExit(1)
        try:
            recorded = record_gap_disposition(host, gap_key, citation=citation, rejection=reject, misfit=misfit)
        except DreamGapLedgerError as exc:
            self.stderr.write(f"  gap-disposition refused: {exc}")
            raise SystemExit(1) from exc
        self.stdout.write(f"  {gap_key}: {recorded} recorded on ticket {host.pk}")

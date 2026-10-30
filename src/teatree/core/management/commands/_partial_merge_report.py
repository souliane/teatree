from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.merge.partial_merge_report import PartialMergeReport, find_partial_merges


class PartialMergeReportCommands(TyperCommand):
    @command(name="partial-merge-report", help="List settled tickets whose issue is still open.")
    def partial_merge_report(
        self,
        *,
        overlay: Annotated[str, typer.Option(help="Limit the report to one overlay.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    ) -> PartialMergeReport:
        report = find_partial_merges(overlay=overlay)
        lines = [
            f"ticket {row['ticket_id']} ({row['state']}, {row['link']}) {row['issue_url']} — "
            f"{row['pr_url']}: {', '.join(row['unmet']) or 'no declared remainder'}"
            for row in report["matches"]
        ]
        lines.extend(f"ticket {row['ticket_id']}: unreadable ({row['reason']})" for row in report["unreadable"])
        self.print_result = False
        emit(
            report,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(lines) if lines else "no settled tickets with an open issue",
        )
        return report

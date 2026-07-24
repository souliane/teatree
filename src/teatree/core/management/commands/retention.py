"""``manage.py retention`` — prune the high-churn control-DB tables (#3693).

``prune`` is DRY-RUN by default: it reports what retention WOULD delete and
touches nothing. Deleting requires the explicit ``--apply`` flag. Both the plan
and the apply share one safety definition (the managers' ``prunable`` querysets):
only rows OLDER than a per-table window whose owning ticket/task is TERMINAL are
ever candidates — a live or in-flight row is never pruned.

The retention windows are the DB-home ``task_attempt_retention_days`` /
``incoming_event_retention_days`` settings (default 30, per-overlay overridable,
``0`` disables that table). Set them with ``t3 <overlay> config_setting set``.
"""

import logging
from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.machine_output import emit
from teatree.core.retention import apply_retention, plan_retention
from teatree.core.table_output import print_table

logger = logging.getLogger(__name__)


class _TableRow(TypedDict):
    table: str
    retention_days: int
    rows: int
    junk: int
    disabled: bool


class RetentionReport(TypedDict):
    applied: bool
    total_rows: int
    tables: list[_TableRow]


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> retention`` group root."""

    @command()
    def prune(
        self,
        *,
        apply: Annotated[
            bool,
            typer.Option("--apply", help="Actually delete the prunable rows. Without it, this is a dry run."),
        ] = False,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the retention report as JSON on stdout instead of the human view."),
        ] = False,
    ) -> None:
        """Prune old rows from the high-churn tables (dry-run unless --apply).

        Conservative: only rows past the retention window whose owning task AND
        ticket are terminal are ever deleted. A live/in-flight row is never touched.
        """
        plan = apply_retention() if apply else plan_retention()
        payload: RetentionReport = {
            "applied": plan.applied,
            "total_rows": plan.total_rows,
            "tables": [
                {
                    "table": table.table,
                    "retention_days": table.retention_days,
                    "rows": table.rows,
                    "junk": table.junk,
                    "disabled": table.disabled,
                }
                for table in plan.tables
            ],
        }
        verb = "Pruned" if apply else "Would prune"
        logger.info("retention: %s %d row(s) across %d table(s)", verb.lower(), plan.total_rows, len(plan.tables))

        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: _render(payload, stream, applied=apply),
        )


def _render(payload: RetentionReport, stream: IO[str], *, applied: bool) -> None:
    verb = "Pruned" if applied else "Would prune"
    rows: list[list[str]] = []
    for table in payload["tables"]:
        if table["disabled"]:
            detail = "disabled (retention_days=0)"
        elif table["junk"]:
            detail = f"{table['rows']} (incl. {table['junk']} park junk), >{table['retention_days']}d, terminal-owned"
        else:
            detail = f"{table['rows']}, >{table['retention_days']}d, terminal-owned"
        rows.append([table["table"], detail])
    title = f"Retention — {verb.lower()} {payload['total_rows']} row(s)"
    if not applied:
        title += " (dry run — pass --apply to delete)"
    print_table(["Table", verb], rows, title=title, stream=stream, justify=["left", "left"])

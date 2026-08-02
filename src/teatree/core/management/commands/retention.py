"""``manage.py retention`` — prune the high-churn control-DB tables (#3693, #3871).

``prune`` is DRY-RUN by default: it reports what retention WOULD delete and
touches nothing. Deleting requires the explicit ``--apply`` flag. Both the plan
and the apply share one safety definition per lane (the managers' ``prunable``
querysets): a row of a LIVE ticket or task is never a candidate. The terminal-owned
lane reaches only rows OLDER than a per-table window. The park lane
(``prunable_parks``) has its own definition — a limit-park audit row carrying no
billed telemetry, aged on ``ended_at`` — because a park RETURNS its task to the
queue, which makes the terminal-owned guard structurally unable to see one.

The retention windows are the DB-home ``task_attempt_retention_days`` /
``park_attempt_retention_days`` / ``incoming_event_retention_days`` (defaults 30 / 7 /
30) and ``task_result_retention_days`` (default 1) settings — per-overlay overridable,
``0`` disables that lane. Set them with ``t3 <overlay> config_setting set``.

The ``TicketTransition`` lane has no window: it fires when the owning ticket CLOSES,
and it removes only rows that are not state edges (``from_state == to_state``), so a
reopened ticket keeps its whole history. Its kill switch is
``ticket_transition_prune_disabled``.

``--apply`` finishes with a ``VACUUM`` (:mod:`teatree.utils.django_db.vacuum`). Deleting
rows on SQLite reclaims no disk on its own — the pages move to the free list and
the file keeps its size — and the control DB is the seed every auto-isolated
worktree env dir is copied from, so its size is paid once per live checkout. A
dry run never vacuums: the rebuild rewrites the whole file, which is not
something a preview may do (#3852). The reclaim it reports is SQLite's own page
delta rather than a file-size difference, because a live reader can defer the
truncation past the rebuild that earned it (#3979).
"""

import logging
from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.machine_output import emit
from teatree.core.retention.prune import PARK_TABLE, apply_retention, plan_retention
from teatree.core.table_output import print_table
from teatree.utils.django_db.vacuum import VacuumOutcome, vacuum_control_db

logger = logging.getLogger(__name__)

_NOT_ATTEMPTED = VacuumOutcome(ran=False, reason="dry run — VACUUM rewrites the file, so it is never previewed")


class _TableRow(TypedDict):
    table: str
    retention_days: int
    rows: int
    junk: int
    disabled: bool
    batches: int
    reason: str
    aged: bool


class _VacuumRow(TypedDict):
    ran: bool
    reason: str
    summary: str
    bytes_reclaimed: int
    page_size: int
    pages_before: int
    pages_after: int
    free_pages_before: int
    free_pages_after: int
    file_bytes_before: int
    file_bytes_after: int
    file_caught_up: bool


def _vacuum_row(vacuum: VacuumOutcome) -> _VacuumRow:
    return {
        "ran": vacuum.ran,
        "reason": vacuum.reason,
        "summary": vacuum.summary,
        "bytes_reclaimed": vacuum.bytes_reclaimed,
        "page_size": vacuum.page_size,
        "pages_before": vacuum.pages_before,
        "pages_after": vacuum.pages_after,
        "free_pages_before": vacuum.free_pages_before,
        "free_pages_after": vacuum.free_pages_after,
        "file_bytes_before": vacuum.file_bytes_before,
        "file_bytes_after": vacuum.file_bytes_after,
        "file_caught_up": vacuum.file_caught_up,
    }


class RetentionReport(TypedDict):
    applied: bool
    total_rows: int
    tables: list[_TableRow]
    vacuum: _VacuumRow


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
        """Prune old rows from the high-churn tables, then reclaim the disk (dry-run unless --apply).

        Conservative: the terminal-owned lane deletes only rows past the retention
        window whose owning task AND ticket are terminal, so a live/in-flight row is
        never touched; the park lane deletes only aged limit-park audit rows that
        carry no billed telemetry.

        On ``--apply`` the deleted pages are handed back to the filesystem with a
        ``VACUUM``, which runs after the prune's transaction has committed because
        it rebuilds the file and so cannot run inside one.
        """
        plan = apply_retention() if apply else plan_retention()
        vacuum = vacuum_control_db() if apply else _NOT_ATTEMPTED
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
                    "batches": table.batches,
                    "reason": table.reason,
                    "aged": table.aged,
                }
                for table in plan.tables
            ],
            "vacuum": _vacuum_row(vacuum),
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


def _detail(table: _TableRow) -> str:
    """The one-line rule that produced this lane's count.

    Each lane names its OWN criteria: reporting the park lane as "terminal-owned"
    would restate the very guard that structurally cannot see a park row, and the
    transition lane never measured an age at all.
    """
    if table["disabled"]:
        return f"disabled ({table['reason'] or 'retention_days=0'})"
    if table["table"] == PARK_TABLE:
        batched = f", {table['batches']} batch(es)" if table["batches"] else ""
        return f"{table['rows']}, limit-park marker, no billed telemetry, >{table['retention_days']}d{batched}"
    if not table["aged"]:
        return f"{table['rows']}, not a state edge, closed-ticket-owned"
    if table["junk"]:
        return f"{table['rows']} (incl. {table['junk']} park junk), >{table['retention_days']}d, terminal-owned"
    return f"{table['rows']}, >{table['retention_days']}d, terminal-owned"


def _render(payload: RetentionReport, stream: IO[str], *, applied: bool) -> None:
    verb = "Pruned" if applied else "Would prune"
    rows: list[list[str]] = [[table["table"], _detail(table)] for table in payload["tables"]]
    rows.append(["VACUUM", payload["vacuum"]["summary"]])
    title = f"Retention — {verb.lower()} {payload['total_rows']} row(s)"
    if not applied:
        title += " (dry run — pass --apply to delete)"
    print_table(["Table", verb], rows, title=title, stream=stream, justify=["left", "left"])

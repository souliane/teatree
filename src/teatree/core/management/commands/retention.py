"""``manage.py retention`` — the retention windows, and the dry runs that precede them.

Three lanes: ``prune`` for the high-churn control-DB tables (#3693, #3871),
``scratch`` for stale agent scratch (#4165), and ``artifacts`` for the checkout pool's
dormant build products (#4244). All three are DRY-RUN by default and delete only under
``--apply``, because each removes something and none of the three is worth running
against a population nobody has read.

The same ``prune`` lanes also run on their own every hour, bounded by a batch budget
(``teatree.loops.timer_reconciler.prune_task_results``). ``prune`` is the operator's view
of that pass: a dry run by default, and under ``--apply`` a drain with no budget followed
by a ``VACUUM``. Plan and apply resolve each lane through the one lane table in
:mod:`teatree.core.retention.prune`, so a row of a live ticket or task is never a candidate.

The windows are the DB-home ``task_attempt_retention_days`` (default 30, never below the
56-day factory lookback, ``0`` disables the task-history lanes) and
``task_result_retention_days`` (default 1, ``0`` disables that lane) settings. Set them
with ``t3 <overlay> config_setting set``; the hourly pass reads the global values, not
per-overlay overrides. The park, ping-payload and ``IncomingEvent``
lanes carry no window setting: they are ``PARK_ATTEMPT_RETENTION_DAYS`` and
``POST_MORTEM_RETENTION_DAYS``.

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

from teatree.config import get_effective_settings, worktree_root
from teatree.core.cleanup.artifact_eviction import (
    ArtifactEvictionPlan,
    EvictionOutcome,
    evict_artifacts,
    plan_artifact_eviction,
)
from teatree.core.machine_output import emit
from teatree.core.retention.prune import (
    COMPLETED_TASK_TABLE,
    FAILED_TASK_TABLE,
    PARK_TABLE,
    PING_PAYLOAD_TABLE,
    TRANSITION_TABLE,
    apply_retention,
    plan_retention,
)
from teatree.core.retention.scratch import ScratchEntry, ScratchSweepPlan, sweep_scratch
from teatree.core.table_output import print_table
from teatree.utils.django_db.vacuum import VacuumOutcome, vacuum_control_db

logger = logging.getLogger(__name__)

_NOT_ATTEMPTED = VacuumOutcome(ran=False, reason="dry run — VACUUM rewrites the file, so it is never previewed")


class _TableRow(TypedDict):
    table: str
    retention_days: int
    rows: int
    cascaded: int
    disabled: bool
    batches: int
    max_batch_ms: int
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
    budget_exhausted: bool
    tables: list[_TableRow]
    vacuum: _VacuumRow


class _ScratchRow(TypedDict):
    path: str
    size_bytes: int
    age_days: float
    removable: bool
    reason: str


class _ArtifactRow(TypedDict):
    path: str
    size_bytes: int
    #: EVICT / STOPPED / KEEP / DEFER — four outcomes a boolean cannot carry, and the one
    #: it used to flatten is the part-deleted artifact an operator must rebuild before use.
    verdict: str
    reason: str


class ArtifactReport(TypedDict):
    applied: bool
    refused: bool
    refusal: str
    idle_days: float
    considered: int
    estimated_bytes: int
    freed_bytes: int
    evicted_count: int
    gaps: list[str]
    entries: list[_ArtifactRow]


class ScratchReport(TypedDict):
    applied: bool
    refused: bool
    root: str
    retention_days: int
    probe_gap: str
    reclaimed_bytes: int
    candidate_bytes: int
    resident_bytes: int
    entries: list[_ScratchRow]


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

        The lanes run in order: aged limit-park attempts; the failed, then
        the completed tasks of finished tickets quiet for the task-history
        window, with their attempts; old notification payloads, blanked so
        dedup holds; settled inbound events; transitions that record no edge;
        finished task results. The same pass runs hourly on its own under a
        batch budget; ``--apply`` drains it with no budget.

        On ``--apply`` the deleted pages are handed back to the filesystem with a
        ``VACUUM``, which runs after the prune's transaction has committed because
        it rebuilds the file and so cannot run inside one.
        """
        plan = apply_retention() if apply else plan_retention()
        vacuum = vacuum_control_db() if apply else _NOT_ATTEMPTED
        payload: RetentionReport = {
            "applied": plan.applied,
            "total_rows": plan.total_rows,
            "budget_exhausted": plan.budget_exhausted,
            "tables": [
                {
                    "table": table.table,
                    "retention_days": table.retention_days,
                    "rows": table.rows,
                    "cascaded": table.cascaded,
                    "disabled": table.disabled,
                    "batches": table.batches,
                    "max_batch_ms": table.max_batch_ms,
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

    @command()
    def artifacts(
        self,
        *,
        days: Annotated[
            float,
            typer.Option("--days", help="Idle window in days. Default: the configured artifact_idle_days."),
        ] = -1.0,
        apply: Annotated[
            bool,
            typer.Option("--apply", help="Actually evict the planned artifacts. Without it, this is a dry run."),
        ] = False,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the eviction report as JSON on stdout instead of the human view."),
        ] = False,
    ) -> None:
        """Reclaim dormant rebuildable build artifacts from the checkout pool (dry-run unless --apply).

        The dry run is the inspection the autonomous pass structurally cannot offer: it
        plans and prints, deleting nothing, so every symlinked artifact, every shared
        symlink target and every artifact the guards could not clear can be READ under
        KEEP. It answers "what would this reclaim on THIS host, and what is it leaving
        alone" — a question worth asking on a host whose overlay points every worktree's
        ``node_modules``/``.venv`` at one directory in the main clone.

        It gates nothing. The autonomous pass runs on its own cadence and deletes only
        what it has PROVED reconstructible, keeping anything it cannot; this command is
        the operator's own view of the same plan, on demand.
        """
        idle_days = days if days >= 0 else get_effective_settings().artifact_idle_days
        plan = plan_artifact_eviction(worktree_root(), idle_days=idle_days)
        outcome = evict_artifacts(plan) if apply and not plan.refusal else EvictionOutcome()
        refusal = plan.refusal or outcome.refusal
        payload: ArtifactReport = {
            "applied": apply,
            "refused": bool(refusal),
            "refusal": refusal,
            "idle_days": idle_days,
            "considered": plan.considered,
            "estimated_bytes": plan.estimated_bytes,
            "freed_bytes": outcome.freed_bytes,
            "evicted_count": len(outcome.evicted),
            "gaps": list(plan.gaps),
            "entries": _artifact_rows(plan, outcome),
        }
        logger.info(
            "retention artifacts: considered %d, %s %d byte(s)",
            plan.considered,
            "freed" if apply else "would free",
            outcome.freed_bytes if apply else plan.estimated_bytes,
        )

        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: _render_artifacts(payload, stream, applied=apply),
        )
        if refusal:
            # The payload is written first: an unattended caller that only sees a
            # non-zero exit with empty streams learns less than the exit 0 it replaces.
            raise SystemExit(1)

    @command()
    def scratch(
        self,
        *,
        root: Annotated[
            str,
            typer.Option("--root", help="Temp root to sweep. Default: the configured scratch_sweep_root."),
        ] = "",
        days: Annotated[
            int,
            typer.Option("--days", help="Retention window. Default: the configured scratch_retention_days."),
        ] = -1,
        apply: Annotated[
            bool,
            typer.Option("--apply", help="Actually reclaim the stale scratch. Without it, this is a dry run."),
        ] = False,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the sweep report as JSON on stdout instead of the human view."),
        ] = False,
    ) -> None:
        """Reclaim stale agent scratch under the temp root (dry-run unless --apply).

        On a RAM-backed ``/tmp`` this is memory, not disk: the measured box held
        8.8 GB of week-old sqlite/venv scratch, 28% of the working pool. An entry
        is reclaimed only when NO file anywhere in its tree was touched inside the
        window (not just the top-level entry's own mtime), it is owned by this
        uid, held open by no live process (fd, cwd, mmap, or a bound AF_UNIX
        socket), and holds no git repository anywhere in its tree — registered or
        ad-hoc — anything the sweep cannot prove stale is kept with the reason
        printed beside it.
        """
        settings = get_effective_settings()
        plan = sweep_scratch(
            configured_root=root or settings.scratch_sweep_root,
            retention_days=days if days >= 0 else settings.scratch_retention_days,
            apply=apply,
        )
        payload: ScratchReport = {
            "applied": plan.applied,
            "refused": plan.refused,
            "root": plan.root,
            "retention_days": plan.retention_days,
            "probe_gap": plan.probe_gap,
            "reclaimed_bytes": plan.reclaimed_bytes,
            "candidate_bytes": plan.candidate_bytes,
            "resident_bytes": plan.resident_bytes,
            "entries": [
                {
                    "path": entry.path,
                    "size_bytes": entry.size_bytes,
                    "age_days": entry.age_days,
                    "removable": entry.removable,
                    "reason": entry.reason,
                }
                for entry in plan.entries
            ],
        }
        logger.info("retention scratch: %s", plan.summary)

        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: _render_scratch(plan, stream, applied=apply),
        )
        if apply and plan.refused:
            # The payload is written first: an unattended caller that only sees a
            # non-zero exit with empty streams learns less than the exit 0 it replaces.
            raise SystemExit(1)


def _artifact_rows(plan: ArtifactEvictionPlan, outcome: EvictionOutcome) -> list[_ArtifactRow]:
    """Every artifact the pass looked at, with the verdict that decided it.

    A stopped candidate carries its OWN reason and its own verdict, never the eligibility
    line it was planned under. Rendering a part-way-failed ``rmtree`` as
    ``KEEP — dormant, rebuildable`` is exactly the "refused means untouched" misreading
    :func:`~teatree.core.cleanup.artifact_removal._failed_delete_state` exists to prevent,
    and it hid that state on the one surface an operator reads.
    """
    stopped = {path: reason for path, _, reason in (line.partition(": ") for line in outcome.skipped)}
    rows: list[_ArtifactRow] = [
        {
            "path": str(candidate.artifact),
            "size_bytes": candidate.size_bytes,
            "verdict": "STOPPED" if str(candidate.artifact) in stopped else "EVICT",
            "reason": stopped.get(str(candidate.artifact), "dormant, rebuildable, nothing depends on it"),
        }
        for candidate in plan.candidates
    ]
    rows += [
        {"path": path, "size_bytes": 0, "verdict": "KEEP", "reason": reason}
        for path, _, reason in (line.partition(": ") for line in plan.kept)
    ]
    rows += [
        {"path": path, "size_bytes": 0, "verdict": "DEFER", "reason": reason}
        for path, _, reason in (line.partition(": ") for line in plan.deferred)
    ]
    return rows


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":  # noqa: PLR2004 — the unit ladder's own base
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _render_artifacts(payload: ArtifactReport, stream: IO[str], *, applied: bool) -> None:
    verb = "Evicted" if applied else "Would evict"
    reclaim = payload["freed_bytes"] if applied else payload["estimated_bytes"]
    title = (
        f"Artifact eviction — considered {payload['considered']}, "
        f"{verb.lower()} {_human_bytes(reclaim)} (idle > {payload['idle_days']:.1f}d)"
    )
    if payload["refused"]:
        if payload["evicted_count"]:
            title += f" — STOPPED after evicting {payload['evicted_count']} artifact(s): {payload['refusal']}"
        else:
            title += f" — REFUSED, nothing was removed: {payload['refusal']}"
    elif not applied:
        title += " (dry run — pass --apply to reclaim)"
    rows = [
        [
            row["path"],
            _human_bytes(row["size_bytes"]),
            f"{row['verdict']} — {row['reason']}",
        ]
        for row in payload["entries"]
    ]
    rows += [["(enumeration)", "", f"ERROR incomplete — {gap}"] for gap in payload["gaps"]]
    print_table(
        ["Artifact", "Size", "Verdict"],
        rows,
        title=title,
        stream=stream,
        justify=["left", "right", "left"],
    )


def _scratch_row(entry: ScratchEntry) -> list[str]:
    return [
        entry.path,
        entry.size_human,
        f"{entry.age_days:.1f}d",
        ("RECLAIM" if entry.removable else "KEEP") + f" — {entry.reason}",
    ]


def _render_scratch(plan: ScratchSweepPlan, stream: IO[str], *, applied: bool) -> None:
    title = f"Scratch retention — {plan.summary}"
    if plan.refused:
        title += " — REFUSED, nothing was removed"
    elif not applied:
        title += " (dry run — pass --apply to reclaim)"
    print_table(
        ["Path", "Size", "Age", "Verdict"],
        [_scratch_row(entry) for entry in plan.entries],
        title=title,
        stream=stream,
        justify=["left", "right", "right", "left"],
    )


#: Each lane names its own rule; the default fits the lanes keyed on a settled row.
_LANE_RULES = {
    PARK_TABLE: "limit-park marker, no billed telemetry",
    FAILED_TASK_TABLE: "quiet finished ticket",
    COMPLETED_TASK_TABLE: "quiet finished ticket",
    PING_PAYLOAD_TABLE: "payload blanked, key and status kept",
    TRANSITION_TABLE: "not a state edge, closed-ticket-owned",
}


def _detail(table: _TableRow) -> str:
    if table["disabled"]:
        return f"disabled ({table['reason'] or 'retention_days=0'})"
    parts = [f"{table['rows']} (+{table['cascaded']} attempt(s))" if table["cascaded"] else str(table["rows"])]
    parts.append(_LANE_RULES.get(table["table"], "terminal-owned"))
    if table["aged"]:
        parts.append(f">{table['retention_days']}d")
    if table["batches"]:
        parts.append(f"{table['batches']} batch(es), longest {table['max_batch_ms']}ms")
    return ", ".join(parts)


def _render(payload: RetentionReport, stream: IO[str], *, applied: bool) -> None:
    verb = "Pruned" if applied else "Would prune"
    rows: list[list[str]] = [[table["table"], _detail(table)] for table in payload["tables"]]
    rows.append(["VACUUM", payload["vacuum"]["summary"]])
    title = f"Retention — {verb.lower()} {payload['total_rows']} row(s)"
    if not applied:
        title += " (dry run — pass --apply to delete)"
    if payload["budget_exhausted"]:
        title += " (stopped at the batch budget; the next pass continues)"
    print_table(["Table", verb], rows, title=title, stream=stream, justify=["left", "left"])

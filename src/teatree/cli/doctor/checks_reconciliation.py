"""Continuous runtime reconciliation ledger — the composition-and-time control (Plan-2 Wave B).

Every other teatree control is *admission control*: a point-in-time judgment on a
single artifact. The failures that hurt — duplicate execution, vacuous eval
gates, park spin, silent loop freezes — are *composition-and-time* failures:
every artifact passed every gate legitimately while a cross-module,
end-to-end invariant was asserted by nobody. Artifact gates are structurally
blind to that class. This module is the control that catches it: a small set of
end-to-end outcome assertions checked against production telemetry, daily,
failing loud to the owner's Slack DM via the existing :func:`notify_user` seam.

Each check is a pure, read-only aggregate over the live control DB — a query,
never a full-table Python scan — folded into a :class:`ReconciliationFinding`
whose ``level`` is ``ok`` (invariant holds), ``alarm`` (invariant violated →
DM'd + surfaced), or ``degraded`` (the read itself crashed → surfaced, never
DM'd, never a false alarm). :func:`reconcile_and_notify` DMs each ``alarm``
under a per-day idempotency key (``reconciliation:<check>:<YYYY-MM-DD>``), so the
watchdog invoking ``t3 doctor`` many times a day still fires at most one DM per
finding per day — the daily cadence is a property of the key, not a separate
scheduler. The doctor registers :func:`_check_reconciliation_ledger` as one
surfacing-only hook: it never reddens the exit code (these are "DM the owner to
investigate" conditions, not "restart the box" conditions).

The duplicate-execution check is the outside cross-validation of Wave A's
idempotency fix: Wave A makes the claim CAS exclude terminal status so no task
row accumulates a second success; this check re-derives "tasks with >1 success"
straight from ``TaskAttempt`` telemetry, so a regression of the CAS is caught
here even if Wave A's own unit tests stay green.
"""

import dataclasses
import datetime as dt

import typer

# ``notify_policy`` is enum-only (Django-free), so it is safe at module load — the
# doctor CLI group loads BEFORE ``ensure_django()``. ``teatree.core.notify`` pulls
# in the ORM, so it is imported lazily inside :func:`_notify_finding` to keep this
# module import-clean under the Django-free CLI, matching the sibling ``checks_*``.
from teatree.core.modelkit.notify_policy import NotifyAudience

#: The 24h operational window most freeze/spin invariants are measured over.
_DAY = dt.timedelta(hours=24)

#: Park attempts per 24h above which the park-spin signature is alarmed. A worker
#: reclaiming its own live task produces a burst of usage-window park rows; a
#: healthy factory parks only during genuine capacity outages, well under this.
MAX_PARK_ROWS_PER_DAY = 200
#: Recorded spend per author-delivered ticket above which redo-work is alarmed.
#: The honest floor is >= $204 (recorded total / deliveries); this alarms at the
#: point cost-per-delivery is unambiguously pathological.
MAX_USD_PER_DELIVERED_TICKET = 1000.0
#: Spend on tickets ending ``ignored``/``not_started`` above which the "spend that
#: reached no shipped ticket" waste is alarmed, measured over :data:`_DEAD_SPEND_WINDOW`.
MAX_USD_ON_DEAD_TICKETS = 250.0
#: The window the dead-ticket spend is measured over. Unwindowed, the check sums
#: every attempt ever recorded: one past runaway pins it red permanently, no action
#: can clear it, and a doctor carrying a stuck red light trains the operator to
#: ignore the findings that ARE actionable. Long enough that a slow burn still
#: accumulates past the floor, short enough that a fixed runaway ages out.
_DEAD_SPEND_WINDOW = dt.timedelta(days=30)
#: Repair-halt escalations per 24h above which repair-loop churn is alarmed.
MAX_HALTS_PER_DAY = 5
#: Age of the oldest unanswered question above which head-of-line intake block is
#: alarmed — one unanswered question stalls the whole intake pipeline.
MAX_OPEN_QUESTION_AGE = dt.timedelta(hours=24)
#: Row-count ceilings for the high-churn control-DB tables above which unbounded
#: growth is surfaced (#3693). Advisory only: a bloated table is a "run the prune"
#: nudge, never a reason to redden the doctor exit code. The park-spin residue
#: reached ~340k rows, so this flags growth well before it becomes operational.
MAX_TASK_ATTEMPT_ROWS = 100_000
MAX_INCOMING_EVENT_ROWS = 100_000
#: #3871: the two tables that actually dominate the control DB. The check reported
#: OK against 3.2M ``TicketTransition`` rows and 625k ``DBTaskResult`` rows because
#: it only counted the two tables that already had a retention lane — the alarm was
#: blind to 85% of the live data by construction.
MAX_TICKET_TRANSITION_ROWS = 100_000
MAX_TASK_RESULT_ROWS = 100_000


class _Level:
    OK = "ok"
    ALARM = "alarm"
    DEGRADED = "degraded"


@dataclasses.dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    """One end-to-end invariant's verdict: healthy, violated (DM'd), or unreadable."""

    check_id: str
    level: str
    message: str

    @property
    def is_alarm(self) -> bool:
        return self.level == _Level.ALARM


def _ok(check_id: str, message: str = "") -> ReconciliationFinding:
    return ReconciliationFinding(check_id=check_id, level=_Level.OK, message=message)


def _alarm(check_id: str, message: str) -> ReconciliationFinding:
    return ReconciliationFinding(check_id=check_id, level=_Level.ALARM, message=message)


def _degraded(check_id: str, exc: Exception) -> ReconciliationFinding:
    return ReconciliationFinding(
        check_id=check_id,
        level=_Level.DEGRADED,
        message=f"reconciliation check `{check_id}` read crashed: {exc.__class__.__name__}: {exc}",
    )


def _now(now: dt.datetime | None) -> dt.datetime:
    if now is not None:
        return now
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    return timezone.now()


def _check_park_spin(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when limit-park OBSERVATIONS in 24h exceed the park-spin threshold.

    Query: ``TaskAttempt`` rows whose ``error`` carries ``LIMIT_PARKED_PREFIX``,
    ``started_at`` within 24h. Threshold: :data:`MAX_PARK_ROWS_PER_DAY`.

    Counts observations, not rows: an unchanged park repeated by a poller folds into one
    row carrying ``park_repeats`` (see ``TaskAttempt.objects.create``), so a row count
    would read the very spin this detector exists to catch as a single quiet row.
    """
    check_id = "park_rows_per_day"
    try:
        from django.db.models import Sum  # noqa: PLC0415 — deferred: keeps this module Django-free at import

        from teatree.core.models import TaskAttempt  # noqa: PLC0415 — ORM import needs the app registry
        from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX  # noqa: PLC0415 — ORM-adjacent constant

        cutoff = _now(now) - _DAY
        parked = TaskAttempt.objects.filter(error__startswith=LIMIT_PARKED_PREFIX, started_at__gte=cutoff)
        count = parked.count() + (parked.aggregate(repeats=Sum("park_repeats"))["repeats"] or 0)
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    if count <= MAX_PARK_ROWS_PER_DAY:
        return _ok(check_id, f"{count} park rows/24h")
    return _alarm(
        check_id,
        f"Park-spin alarm: `{count}` task attempts limit-parked in the last 24h "
        f"(alarm above `{MAX_PARK_ROWS_PER_DAY}`). This is the park-spin signature — a worker "
        f"reclaiming its own live task. Check loop cadence with `t3 loops` and the usage-window state.",
    )


def _check_cost_per_delivered_ticket(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when recorded spend per author-delivered ticket exceeds the floor.

    Query: ``sum(TaskAttempt.cost_usd)`` (lifetime, the whole priced surface) over
    the count of author tickets in ``{DELIVERED, MERGED}``. Threshold:
    :data:`MAX_USD_PER_DELIVERED_TICKET`. Spend with zero deliveries is itself an
    alarm (every dollar reached no delivery).

    Deliberately unwindowed, and not a ratchet: this is a running average whose
    denominator grows with the numerator, so every delivery cheaper than the current
    average drags it back down — normal operation clears the alarm. Windowing would
    make it worse, not better. ``Ticket`` carries no timestamp, so only the numerator
    could be windowed, and windowed spend over lifetime deliveries trends to zero —
    a check that can never fire.
    """
    del now  # a self-correcting running average, not a windowed aggregate; taken for uniform dispatch
    check_id = "cost_per_delivered_ticket"
    try:
        from django.db.models import Sum  # noqa: PLC0415 — deferred: Django import at call time

        from teatree.core.models import TaskAttempt, Ticket  # noqa: PLC0415 — ORM import needs the app registry

        spend = TaskAttempt.objects.aggregate(total=Sum("cost_usd"))["total"] or 0.0
        delivered = Ticket.objects.filter(
            role=Ticket.Role.AUTHOR,
            state__in=[Ticket.State.DELIVERED, Ticket.State.MERGED],
        ).count()
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    if spend <= 0.0:
        return _ok(check_id, "no recorded spend")
    if delivered == 0:
        return _alarm(
            check_id,
            f"Cost-per-delivered-ticket alarm: `${spend:.2f}` of recorded spend reached "
            f"`0` author-delivered tickets. Every dollar was spent on work that never delivered — "
            f"see the duplicate-execution and dead-ticket-spend checks.",
        )
    per = spend / delivered
    if per <= MAX_USD_PER_DELIVERED_TICKET:
        return _ok(check_id, f"${per:.2f}/delivered ticket")
    return _alarm(
        check_id,
        f"Cost-per-delivered-ticket alarm: `${per:.2f}` per author-delivered ticket "
        f"(`${spend:.2f}` spend / `{delivered}` delivered; alarm above `${MAX_USD_PER_DELIVERED_TICKET:.0f}`). "
        f"Redo-work is inflating spend — cross-check the duplicate-execution alarm.",
    )


def _check_dead_ticket_spend(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when spend on tickets ending ``ignored``/``not_started`` exceeds the floor.

    Query: ``sum(TaskAttempt.cost_usd)`` joined to tickets in
    ``{IGNORED, NOT_STARTED}``, over attempts started within
    :data:`_DEAD_SPEND_WINDOW`. Threshold: :data:`MAX_USD_ON_DEAD_TICKETS`.

    The window hangs off ``TaskAttempt.started_at`` — the row being summed — not
    off ``Ticket``, which carries no timestamp. Summing lifetime made this a
    ratchet: it could trip once and then never clear.
    """
    check_id = "dead_ticket_spend"
    try:
        from django.db.models import Sum  # noqa: PLC0415 — deferred: Django import at call time

        from teatree.core.models import TaskAttempt, Ticket  # noqa: PLC0415 — ORM import needs the app registry

        spend = (
            TaskAttempt.objects.filter(
                task__ticket__state__in=[Ticket.State.IGNORED, Ticket.State.NOT_STARTED],
                started_at__gte=_now(now) - _DEAD_SPEND_WINDOW,
            ).aggregate(total=Sum("cost_usd"))["total"]
            or 0.0
        )
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    if spend <= MAX_USD_ON_DEAD_TICKETS:
        return _ok(check_id, f"${spend:.2f} on dead tickets")
    return _alarm(
        check_id,
        f"Dead-ticket-spend alarm: `${spend:.2f}` spent in the last "
        f"`{_DEAD_SPEND_WINDOW.days}d` on tickets ending `ignored`/`not_started` "
        f"(alarm above `${MAX_USD_ON_DEAD_TICKETS:.0f}`) — spend that reached no shipped ticket.",
    )


def _check_enabled_loops_ticked(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when a verdict-ADMITTED loop has not ticked in 24h (a silent freeze).

    Query: every ``Loop`` row the effective verdict admits (hold > forced > preset >
    ``Loop.enabled``) whose ``last_run_at`` is null or older than 24h. Keyed on the raw
    ``enabled`` column instead, a preset-forced-on loop froze invisibly — this bug's exact
    signature — and a preset-masked-off enabled loop false-alarmed (#4185). Deliberately
    NOT intersected with the live-tick registry: an ``off_live_tick`` loop has its own
    driver chain and freezes just as silently.
    """
    check_id = "enabled_loops_ticked_24h"
    try:
        from teatree.core.models import Loop  # noqa: PLC0415 — ORM import needs the app registry
        from teatree.loops.preset_status import effective_verdicts  # noqa: PLC0415 — ORM-backed resolver

        admitted = {verdict.name for verdict in effective_verdicts() if verdict.admitted}
        cutoff = _now(now) - _DAY
        stale = sorted(
            row.name
            for row in Loop.objects.filter(name__in=admitted).only("name", "last_run_at")
            if row.last_run_at is None or row.last_run_at < cutoff
        )
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    if not stale:
        return _ok(check_id, "all admitted loops ticked in 24h")
    names = ", ".join(f"`{name}`" for name in stale)
    return _alarm(
        check_id,
        f"Loop-freeze alarm: {len(stale)} admitted loop(s) have not ticked in 24h: {names}. "
        f"An admitted loop that stops ticking is a silent freeze — start the worker "
        f"(`t3 worker ensure`) or inspect `t3 loops`.",
    )


def _check_vacuous_eval_gates(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when an eval run in 24h reported green having graded zero scenarios.

    Query: ``EvalRunRecord`` started within 24h whose graded (non-skip/non-error)
    scenario-result count is zero. A green check that ran no real case proves
    nothing — the vacuous-gate signature.
    """
    check_id = "green_ci_check_ran_a_case"
    try:
        from teatree.core.models import EvalRunRecord  # noqa: PLC0415 — ORM import needs the app registry

        cutoff = _now(now) - _DAY
        vacuous = sorted(
            run.pk for run in EvalRunRecord.objects.filter(started_at__gte=cutoff) if run.results.graded().count() == 0
        )
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    if not vacuous:
        return _ok(check_id, "no vacuous eval runs in 24h")
    ids = ", ".join(f"`#{pk}`" for pk in vacuous)
    return _alarm(
        check_id,
        f"Vacuous-gate alarm: {len(vacuous)} eval run(s) in 24h reported green having graded "
        f"`0` scenarios (run id(s) {ids}). A green check that executed zero real cases proves nothing.",
    )


def _check_halt_count(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when repair-halt escalations in 24h exceed the threshold.

    Query: ``DeferredQuestion`` whose ``dedupe_marker`` starts ``repair-`` (the
    repair-stall / repair-cap escalations), ``created_at`` within 24h. Threshold:
    :data:`MAX_HALTS_PER_DAY`.
    """
    check_id = "halt_count_24h"
    try:
        from teatree.core.models import DeferredQuestion  # noqa: PLC0415 — ORM import needs the app registry

        cutoff = _now(now) - _DAY
        count = DeferredQuestion.objects.filter(dedupe_marker__startswith="repair-", created_at__gte=cutoff).count()
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    if count <= MAX_HALTS_PER_DAY:
        return _ok(check_id, f"{count} repair-halts/24h")
    return _alarm(
        check_id,
        f"Repair-halt alarm: `{count}` repair-halt escalation(s) recorded in 24h "
        f"(alarm above `{MAX_HALTS_PER_DAY}`). Tickets are stalling in repair loops — "
        f"triage via `t3 teatree questions list`.",
    )


def _check_open_question_age(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when the oldest unanswered question has waited past the threshold.

    Query: oldest ``DeferredQuestion.pending()`` row. Threshold:
    :data:`MAX_OPEN_QUESTION_AGE`. One unanswered question can head-of-line block
    the entire intake pipeline.
    """
    check_id = "open_question_age"
    try:
        from teatree.core.models import DeferredQuestion  # noqa: PLC0415 — ORM import needs the app registry

        oldest = DeferredQuestion.pending().first()
        moment = _now(now)
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    if oldest is None:
        return _ok(check_id, "no open questions")
    age = moment - oldest.created_at
    if age <= MAX_OPEN_QUESTION_AGE:
        return _ok(check_id, f"oldest open question {age.total_seconds() / 3600:.1f}h")
    hours = age.total_seconds() / 3600
    threshold_hours = MAX_OPEN_QUESTION_AGE.total_seconds() / 3600
    return _alarm(
        check_id,
        f"Open-question-age alarm: the oldest unanswered question has waited `{hours:.1f}h` "
        f"(alarm above `{threshold_hours:.0f}h`). One unanswered question can head-of-line block "
        f"intake — answer via `t3 teatree questions list`.",
    )


def _check_duplicate_execution(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when any task recorded more than one successful attempt in 24h.

    Query: ``TaskAttempt`` rows with ``outcome=SUCCESS`` and ``started_at`` within
    24h, grouped by ``task_id`` having ``count > 1``. Any such task alarms — this
    is the F4/idempotency signature and the OUTSIDE cross-validation of Wave A's
    claim-CAS fix (which should keep this at zero).
    """
    check_id = "duplicate_execution_count"
    try:
        from django.db.models import Count  # noqa: PLC0415 — deferred: Django import at call time

        from teatree.core.models import TaskAttempt  # noqa: PLC0415 — ORM import needs the app registry

        cutoff = _now(now) - _DAY
        count = (
            TaskAttempt.objects.filter(outcome=TaskAttempt.Outcome.SUCCESS, started_at__gte=cutoff)
            .values("task_id")
            .annotate(successes=Count("id"))
            .filter(successes__gt=1)
            .count()
        )
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    if count == 0:
        return _ok(check_id, "no duplicate-execution tasks")
    return _alarm(
        check_id,
        f"Duplicate-execution alarm: `{count}` task(s) recorded more than one successful attempt "
        f"in 24h — the idempotency-boundary signature (a task double-executed). This cross-checks the "
        f"Wave A claim-CAS fix from the outside; a non-zero count means the admission gate is not holding.",
    )


def _check_review_dispatch_saturation(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when a review, codex or critic claim spent its whole retry budget with no verdict.

    Query: ``AutoReviewDispatch.saturated()`` + ``CodexReviewMarker.saturated()`` +
    ``CriticDispatch.saturated()`` — claims still in ``dispatched`` whose deadline
    has passed and whose ``attempts`` reached the bound. Any such row alarms.

    This is the visible end of the bounded retry (#3920, #3921). A dispatch that
    expires is re-armed, so an ordinary crashed reviewer never reaches here; a
    saturated claim means every attempt died and nothing will arm that head
    again. Without this the bound would be a SILENT deadlock — the same failure
    class as the unbounded version it replaced, only quieter.
    """
    check_id = "review_dispatch_saturation"
    try:
        from teatree.core.models import (  # noqa: PLC0415 — ORM import needs the app registry
            AutoReviewDispatch,
            CodexReviewMarker,
            CriticDispatch,
        )

        moment = _now(now)
        reviews = [f"`{row.slug}#{row.pr_id}@{row.head_sha[:8]}`" for row in AutoReviewDispatch.saturated(at=moment)]
        codex = [f"`codex:{row.slug}#{row.pr_id}@{row.head_sha[:8]}`" for row in CodexReviewMarker.saturated(at=moment)]
        critics = [
            f"`ticket:{row.ticket_id}/{row.transition}@{row.head_sha[:8]}`"
            for row in CriticDispatch.saturated(at=moment)
        ]
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    stranded = reviews + codex + critics
    if not stranded:
        return _ok(check_id, "no saturated review/critic dispatches")
    return _alarm(
        check_id,
        f"Review-dispatch saturation alarm: {len(stranded)} claim(s) exhausted the retry budget "
        f"with no verdict recorded: {', '.join(stranded)}. Every armed review/critic for those heads "
        f"died, so nothing will re-arm them and the merge gate stays unsatisfied — review each by hand "
        f"or push a new commit to arm a fresh head.",
    )


def _high_churn_counts() -> dict[str, tuple[int, int]]:
    """``{table: (rows, ceiling)}`` for every table retention has a lane over."""
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    from teatree.core.models import IncomingEvent, TaskAttempt  # noqa: PLC0415 — ORM import needs the app registry
    from teatree.core.models.transition import TicketTransition  # noqa: PLC0415 — ORM import needs the app registry

    return {
        "TaskAttempt": (TaskAttempt.objects.count(), MAX_TASK_ATTEMPT_ROWS),
        "IncomingEvent": (IncomingEvent.objects.count(), MAX_INCOMING_EVENT_ROWS),
        "TicketTransition": (TicketTransition.objects.count(), MAX_TICKET_TRANSITION_ROWS),
        "DBTaskResult": (DBTaskResult.objects.count(), MAX_TASK_RESULT_ROWS),
    }


def _check_high_churn_table_size(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when a high-churn table's row count exceeds its ceiling (#3693, #3871).

    Covers exactly the tables retention has a lane over, so the alarm and the
    prescribed remedy stay in step: a table the check flags is always one
    ``retention prune`` can act on. Advisory — surfaces DB growth before it bites.
    """
    del now  # a current-state row count, not a time window; taken for uniform dispatch
    check_id = "high_churn_table_size"
    try:
        counts = _high_churn_counts()
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    over = [
        f"`{table}` at `{rows}` (ceiling `{ceiling}`)" for table, (rows, ceiling) in counts.items() if rows > ceiling
    ]
    if not over:
        return _ok(check_id, " / ".join(f"{rows} {table}" for table, (rows, _) in counts.items()))
    return _alarm(
        check_id,
        f"High-churn table size alarm: {', '.join(over)}. The control DB is growing unbounded — "
        f"prune terminal-owned old rows with `t3 <overlay> retention prune` (dry-run) then "
        f"`t3 <overlay> retention prune --apply`.",
    )


#: Every reconciliation check, in a stable report order.
CHECKS: tuple = (
    _check_park_spin,
    _check_cost_per_delivered_ticket,
    _check_dead_ticket_spend,
    _check_enabled_loops_ticked,
    _check_vacuous_eval_gates,
    _check_halt_count,
    _check_open_question_age,
    _check_duplicate_execution,
    _check_high_churn_table_size,
    _check_review_dispatch_saturation,
)


def run_reconciliation_checks(now: dt.datetime | None = None) -> list[ReconciliationFinding]:
    """Run every reconciliation check and return its findings, stable order."""
    moment = _now(now)
    return [check(moment) for check in CHECKS]


def _notify_finding(finding: ReconciliationFinding, *, day: dt.date) -> None:
    """DM one alarm to the owner, daily-deduped by the per-day idempotency key.

    Best-effort: the notify seam no-ops when no Slack backend is configured
    (dev boxes, CI) and an unexpected error must never break the doctor run.
    """
    from teatree.core.notify import (  # noqa: PLC0415 — deferred: keeps the module Django-free at CLI load
        NotifyKind,
        notify_user,
    )

    try:
        notify_user(
            finding.message,
            kind=NotifyKind.INFO,
            idempotency_key=f"reconciliation:{finding.check_id}:{day.isoformat()}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:  # noqa: BLE001 — the loud channel is best-effort; never break the doctor run
        typer.echo(f"WARN  Reconciliation DM failed for `{finding.check_id}` (surfaced locally instead).")


def reconcile_and_notify(now: dt.datetime | None = None) -> list[ReconciliationFinding]:
    """Run the ledger and DM each alarm (once per day per check). Returns findings."""
    moment = _now(now)
    findings = run_reconciliation_checks(moment)
    for finding in findings:
        if finding.is_alarm:
            _notify_finding(finding, day=moment.date())
    return findings


def _check_reconciliation_ledger() -> bool:
    """Doctor hook: run the reconciliation ledger, DM alarms, surface findings.

    Surfacing-only — always returns ``True``: a violated end-to-end invariant is
    a "DM the owner to investigate" condition, not a "restart the box" one, so it
    must never redden the exit code the watchdog keys on. The loud channel is the
    daily-deduped owner DM; the ``WARN`` echoes below keep the same findings
    visible in ``t3 doctor`` / its ``--json`` surface without flipping RED.
    """
    try:
        findings = reconcile_and_notify()
    except Exception as exc:  # noqa: BLE001 — the ledger must never crash the doctor run
        typer.echo(f"WARN  Reconciliation ledger crashed: {exc.__class__.__name__}: {exc}")
        return True
    for finding in findings:
        if finding.level in {_Level.ALARM, _Level.DEGRADED}:
            typer.echo(f"WARN  {finding.message}")
    return True

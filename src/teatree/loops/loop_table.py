"""Loop-table fan-out — dispatch each row via its OWN load-bearing column (#1796, #2513, #2584).

The cutover from the code-cadence tick: the fan-out no longer consults a
code-cadence ledger to decide whether a mini-loop should fire — the DB ``Loop``
row carries cadence + the enable toggle, and ``Loop.last_run_at`` is the single
cadence ledger. #2584 closes the gap the #2513 cutover opened: a loop runs this
tick iff it is NOT ``off_live_tick`` AND its ``Loop`` row is ``is_due(now)`` (its
own ``delay_seconds`` interval, or its ``daily_at`` wall-clock schedule) AND the
combined enable verdict admits it. That verdict is
:func:`teatree.loop.loop_state_db.loop_state_admits` — ``Loop.enabled`` (the
configured/opt-in plane) AND not ``LoopState``-held (the durable runtime control
tier: ``t3 loop pause`` / ``disable``, #1913) — there is no env kill-switch and no
``[loops]`` toml disabled-state tier. The tick applies that ONE predicate over its
already-bulk-loaded ``Loop`` rows plus a SINGLE bulk ``LoopState`` read (no
per-loop hold query), and the standalone :func:`loop_enabled` single-lookup used
by the off-live-tick daily loop gates applies the same predicate — so no
enable-decision site drifts into a tier-subset. (The review-claim chokepoint
reads the ``LoopState`` arm only, by documented design — see
:mod:`teatree.loop.loop_state_db`.)

**The ``script``/``prompt`` column is LOAD-BEARING (#2513 regression fix).** The
fan-out no longer selects an admitted row's behaviour by a name-only registry
lookup (the regression that left the DB ``script`` column dead). For each admitted
row it READS the column: a **script** row's ``script`` is resolved to the loop's
OWN name (:func:`teatree.loops.run.parse_script_loop_name`) and THAT loop's
``build_jobs`` fans out — a row whose ``script`` does not resolve to a real
registered loop module raises and is logged + skipped (never a silent no-op); a
**prompt** row dispatches its own loop's ``build_jobs`` (its scanner queues the
prompt-instructed work). So which behaviour fans out is decided by the row's
column, not by its name.

An ``off_live_tick`` loop (the heavy ``dream`` distiller pass, #1933 § 3) is
NEVER picked up here — the live tick must not invoke its ``build_jobs`` or bump
its ``last_run_at``; it is driven by
:func:`teatree.loops.off_live_tick_driver.drive_off_live_tick_loops` firing its own
tick command. The combined
enable verdict (the ``LoopState`` hold check) runs BEFORE the cadence claim so a
held loop is neither dispatched nor cadence-bumped — its anchor is preserved, not
silently consumed. The fan-out then ATOMICALLY claims an admitted loop's ``last_run_at``
(a compare-and-swap on the anchor it read, :meth:`LoopManager.mark_run_if_unchanged`)
BEFORE building its jobs, so two ticks that read the same anchor cannot both
drive the loop — exactly one wins the claim and dispatches.

**Colleague-facing loops defer while the mode is unreachable (#2904, #61).** A row
with ``Loop.colleague_facing`` set is additionally gated on the single active
:class:`~teatree.core.mode_resolution.ResolvedMode`: whenever the resolved mode
``defers_questions`` (an away-class mode — the same axis that defers user-directed
questions), the row is NOT admitted, cadence-bumped, or dispatched — colleague-facing
work should not fire while the user is unreachable to weigh in, even in an
autonomous-away mode where every other loop keeps self-pumping (the
``pauses_self_pump``/``defers_questions`` split). The loop mask AND the
availability posture now come from the SAME resolved mode (the #61 merge), so the
two can never drift. Auto-merge under away is preserved as loop membership, not an
availability read: ``pr_sweep`` is a non-``colleague_facing`` ship-domain scanner,
so it keeps running while the review loop (``colleague_facing``) is deferred.

This is the ``jobs_builder`` the per-loop tick (``t3 loops tick --loop <name>``)
injects into the shared :func:`teatree.loop.tick.run_tick` pipeline, so reap +
scan + act + render are reused unchanged — only the gate (which loops run, on
whose cadence) moves from code into the DB rows + the unified verdict.

**Every refusal carries its reason (#3843).** The fan-out's primary form is
:func:`dispatch_loop_table`, which returns one :class:`LoopDispatch` per
considered loop — its jobs, or a one-line reason it did not run.
:func:`build_loop_table_jobs` is the flat-job-list projection of it. The
distinction is load-bearing, not cosmetic: a refused loop and a loop that fanned
out and found no work BOTH contribute zero jobs, so a caller reading only the
list renders a control-plane hold as a healthy quiet tick. That is precisely how
a force-OFF ``review`` loop reported ``ran … 0 signal(s), 0 action(s)`` for hours
while no PR was ever cold-reviewed.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.loop.job_identity import _ScannerJob
from teatree.loops.base import BuildJobsContext, MiniLoop
from teatree.loops.enable_verdict import EnablePlanes
from teatree.loops.registry import iter_loops

if TYPE_CHECKING:
    from teatree.core.mode_resolution import ResolvedMode
    from teatree.core.models import Loop

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _TickAdmission:
    """The per-tick inputs the unified verdict shares across every loop this pass.

    The enable planes are :class:`~teatree.loops.enable_verdict.EnablePlanes` — the SAME
    object :func:`teatree.loops.chain_membership.timer_chain_loop_names` asks — so the
    tick's per-fire admission and the chain membership it is driven by cannot disagree
    (#4185). Resolved ONCE per tick so a fan-out of N loops issues those reads once, not
    per loop (#2584 / #3159 / #61); the cadence and reach arms this module adds on top
    are what make admission NARROWER than membership, never differently-sourced.
    """

    now: dt.datetime
    planes: EnablePlanes

    @classmethod
    def resolve(cls, now: dt.datetime) -> "_TickAdmission":
        return cls(now=now, planes=EnablePlanes.resolve(now))

    @property
    def resolved(self) -> "ResolvedMode":
        return self.planes.resolved


@dataclass(frozen=True, slots=True)
class LoopDispatch:
    """One loop's outcome in a loop-table pass — its jobs, or why it did not run (#3843).

    ``blocked_reason`` is the empty string exactly when the loop dispatched, and
    otherwise a one-line, operator-actionable statement of what refused it. It is
    the fact a bare job list cannot carry: a refused loop and a loop that fanned
    out and found no work both contribute zero jobs, so a caller reading only the
    list renders both as a healthy quiet tick.
    """

    name: str
    jobs: tuple["_ScannerJob", ...] = ()
    blocked_reason: str = ""

    @property
    def dispatched(self) -> bool:
        """Whether the loop actually fanned its scanners out this pass."""
        return not self.blocked_reason


def loop_block_reasons(now: dt.datetime, *, rows: "dict[str, Loop] | None" = None) -> dict[str, str]:
    """Per registered mini-loop, why the live tick would refuse it at *now* — ``""`` if not.

    The read-only projection of the admission gate the fan-out itself applies: it
    resolves the same per-tick inputs and calls the same :func:`_admission_block`,
    so a reader (the dashboard's live-work view) reports the tick's OWN reason
    rather than a second vocabulary that can drift from the decision.

    Keyed on the REGISTRY, not on ``Loop`` rows: a ``Loop`` row with no registered
    mini-loop is not something the live tick dispatches at all, while a registered
    loop with no row is a real, reportable misconfiguration — which is exactly the
    asymmetry ``_admission_block`` already states.

    Nothing here mutates: no cadence anchor is claimed and no ``build_jobs`` runs,
    so asking the question never has the side effect of answering it differently
    next time.

    *rows* lets a caller that ALREADY holds the ``Loop`` rows (a polled dashboard
    panel rendering the same anchors beside the reason) pass them in rather than
    pay for a second read of the same table on every poll.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    admission = _TickAdmission.resolve(now)
    by_name = {row.name: row for row in Loop.objects.all()} if rows is None else rows
    return {loop.name: _admission_block(by_name.get(loop.name), loop, admission) for loop in iter_loops()}


def _resolve_dispatch_loop(row: "Loop", registry_by_name: dict[str, MiniLoop]) -> MiniLoop:
    """The mini-loop an admitted ``row`` dispatches — decided by its column, not its name.

    A **script** row's ``Loop.script`` is parsed UP to the loop's own name
    (:func:`teatree.loops.run.parse_script_loop_name`) and that mini-loop is
    looked up in the per-tick registry; a stale/shared ``script`` (not the
    per-loop module shape) raises
    :class:`teatree.loops.run.UnresolvableScriptError` LOUDLY, and a name with no
    registry entry raises ``KeyError`` — both surface as a loud failure the fan-out
    logs and skips, never a silent no-op. A **prompt** row dispatches its own
    registered mini-loop.
    """
    from teatree.loops.run import parse_script_loop_name  # noqa: PLC0415 — deferred: loaded at tick time, not import

    target = parse_script_loop_name(row.script) if row.script else row.name
    return registry_by_name[target]


def _admission_block(row: "Loop | None", loop: MiniLoop, ctx: _TickAdmission) -> str:
    """Why *loop* is NOT admitted this tick — the empty string when it IS (#3843).

    The single source of BOTH the admission verdict and its explanation: a
    non-empty return is exactly the condition :func:`_loop_admitted` refuses on,
    phrased as a one-line, operator-actionable reason. Holding the two in one
    function is what stops the reported reason from drifting away from the
    decision that produced it — and a reason is what the console needs, because
    a refused loop and a loop that swept and found nothing both yield an empty
    job list. A force-OFF ``review`` loop printing ``ran … 0 signal(s)`` for
    hours is the outage this exists to make impossible.

    Split in two along the same seam the verdict itself has: the schedule/reach
    arms this function owns, and the enable planes
    :func:`_control_plane_block` explains.
    """
    if loop.off_live_tick:
        return "off_live_tick — driven by its own tick command, never the live tick"
    if row is None:
        return "no Loop row — this loop's config was never seeded"
    if not row.is_due(ctx.now):
        return "not due yet on its own cadence"
    if row.colleague_facing and ctx.resolved.defers_questions:
        return f"colleague-facing while mode {ctx.resolved.name!r} defers questions"
    return _control_plane_block(row, loop, ctx)


def _control_plane_block(row: "Loop", loop: MiniLoop, ctx: _TickAdmission) -> str:
    """Which enable plane refused *loop* — the empty string when none did.

    Delegated whole to :meth:`~teatree.loops.enable_verdict.EnablePlanes.refusal`, the
    one owner of both the enable boolean and its explanation, so the tick's gate and
    the chain membership built from the same seam can never drift apart. PURE over
    *ctx*'s already-bulk-loaded planes — it issues NO query of its own, so the
    single-``teatree_loop_state``-read invariant (#2584) holds unchanged.
    """
    return ctx.planes.refusal(loop.name, configured_enabled=row.enabled)


def _loop_admitted(row: "Loop | None", loop: MiniLoop, ctx: _TickAdmission) -> bool:
    """The unified enabled+due+reachable verdict for one loop — no cadence claim.

    A loop is admitted iff it is NOT ``off_live_tick`` (those loops are driven by
    :func:`teatree.loops.off_live_tick_driver.drive_off_live_tick_loops`), it HAS a ``Loop``
    row that is
    ``is_due(now)``, it is NOT ``colleague_facing`` while *ctx.resolved*
    ``defers_questions`` (holiday-``away`` / ``autonomous_away``, #2904), AND
    :class:`~teatree.loops.enable_verdict.EnablePlanes` admits it — not held (the bulk
    ``LoopState`` read, #2584), then the forced plane, then the active mode's mask over
    ``Loop.enabled``. Those planes are the SAME object chain membership reads (#4185),
    so :func:`build_loop_table_jobs`, :func:`admitted_loop_names` and
    :func:`teatree.loops.chain_membership.timer_chain_loop_names` cannot drift.

    Derived from :func:`_admission_block` (admitted ⇔ no block), so the boolean
    the timer chain gates on and the reason the console prints are one decision.
    """
    return not _admission_block(row, loop, ctx)


def admitted_loop_names(now: dt.datetime, *, only: str | None = None) -> list[str]:
    """Names of every loop the unified verdict admits (enabled + due + un-held) — NO cadence claim.

    The loop-timer chain's admission pre-filter (#1796): it asks the SAME unified
    verdict :func:`build_loop_table_jobs` uses (via :func:`_loop_admitted`) but never
    claims the cadence anchor. The atomic ``mark_run_if_unchanged`` CAS stays in the
    per-loop tick the timer runs, so an at-least-once double delivery is a no-op
    there — the timer's admission step only ASKS whether the row is due, it never
    drives one.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    admission = _TickAdmission.resolve(now)
    rows = {row.name: row for row in Loop.objects.all()}
    return [
        loop.name
        for loop in iter_loops()
        if (only is None or loop.name == only) and _loop_admitted(rows.get(loop.name), loop, admission)
    ]


def build_loop_table_jobs(
    scanner_context: BuildJobsContext, *, now: dt.datetime, only: str | None = None
) -> list[_ScannerJob]:
    """The flat scanner-job list of :func:`dispatch_loop_table` — the wire-compatible form.

    Every gate, cadence claim and dispatch rule lives in
    :func:`dispatch_loop_table`; this is the projection callers that only need
    the jobs consume. A caller that must also tell "the control plane refused
    this loop" from "the loop ran and found nothing" reads the outcomes instead.
    """
    return [job for outcome in dispatch_loop_table(scanner_context, now=now, only=only) for job in outcome.jobs]


def dispatch_loop_table(
    scanner_context: BuildJobsContext, *, now: dt.datetime, only: str | None = None
) -> list[LoopDispatch]:
    """One :class:`LoopDispatch` per considered loop — its jobs, or the reason it did not run.

    An ``off_live_tick`` loop (the heavy ``dream`` pass, #1933 § 3) is skipped
    first, before any DB work — the live tick must never invoke its ``build_jobs``
    or bump its ``last_run_at``. A registry mini-loop with no ``Loop`` row is
    skipped (its config was never seeded). A loop whose row is disabled or
    not-due is skipped; a ``colleague_facing`` row is skipped while availability
    defers questions (#2904); and a loop the combined verdict holds — a
    ``LoopState`` PAUSED/DISABLED row in the single bulk read (#1913, #2584) — is
    skipped too, ALL BEFORE ``mark_run``, so a held loop's cadence anchor is
    preserved.

    ``only`` (#2650) scopes the build to a SINGLE named loop — the per-loop
    ``/loop`` fires ``t3 loops tick --loop <name>``, so exactly that one row is
    considered (every other row is untouched, its cadence anchor unconsumed). The
    same enabled / due / unified-verdict gates still apply to that one row.

    Each admitted row's cadence anchor is claimed atomically
    (:meth:`LoopManager.mark_run_if_unchanged`, a CAS on the ``last_run_at`` the
    row was read with) BEFORE its jobs are built, so two ticks that read the same
    anchor never both drive the loop — the loser's CAS matches 0 rows and it
    skips. The dispatch target is then read from the row's OWN ``script``/``prompt``
    column (#2513): a script row's ``script`` resolves to the loop it names, a
    prompt row dispatches its own loop. A row whose ``script`` does not resolve to
    a real registered loop module raises — that one loop is logged and skipped
    (never aborts the tick, never a silent no-op). Because the anchor is claimed
    before ``build_jobs``, a row that wins the claim but then raises has already
    advanced its anchor (it is simply not re-driven until its cadence elapses
    again).

    Every one of those skips records its reason on the loop's
    :class:`LoopDispatch` (#3843) rather than vanishing into an empty job list,
    so a caller can report WHY a loop produced nothing instead of reporting a
    refusal as a successful, quiet tick.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    admission = _TickAdmission.resolve(now)
    registry = tuple(iter_loops())
    registry_by_name = {loop.name: loop for loop in registry}
    rows = {row.name: row for row in Loop.objects.all()}
    outcomes: list[LoopDispatch] = []
    for loop in registry:
        if only is not None and loop.name != only:
            continue
        blocked = _admission_block(rows.get(loop.name), loop, admission)
        if blocked:
            outcomes.append(LoopDispatch(name=loop.name, blocked_reason=blocked))
            continue
        row = rows[loop.name]
        # Atomically claim the cadence anchor BEFORE building jobs so two ticks
        # that read the same ``last_run_at`` cannot both drive the loop
        # (lost-update double-drive). The loser's CAS matches 0 rows and it skips.
        # The anchor advances ahead of ``build_jobs`` — benign for a raising loop
        # (it is not re-driven until its cadence elapses again), the price of
        # atomicity.
        if not Loop.objects.mark_run_if_unchanged(loop.name, previous_last_run_at=row.last_run_at, now=now):
            outcomes.append(
                LoopDispatch(name=loop.name, blocked_reason="another tick claimed this loop's cadence anchor first"),
            )
            continue
        try:
            target = _resolve_dispatch_loop(row, registry_by_name)
            built = target.build_jobs(**scanner_context)
        except Exception:
            logger.exception("Loop %r raised while resolving/building jobs from its column — skipping", loop.name)
            outcomes.append(
                LoopDispatch(name=loop.name, blocked_reason="its build_jobs raised — see the loop log"),
            )
            continue
        outcomes.append(LoopDispatch(name=loop.name, jobs=tuple(built)))
    return outcomes

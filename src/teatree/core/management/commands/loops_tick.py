"""``manage.py loops_tick`` — one PER-LOOP tick of a single DB ``Loop`` row (#2650).

The single tick surface for ``t3 loops tick --loop <name>``: the per-loop
primitive each native Claude ``/loop`` fires. **There is NO master tick.** The
loop is PER-LOOP ONLY — one native Claude ``/loop`` per enabled ``Loop`` row, each
firing this command scoped to its own row on its own cadence. Invoking
``t3 loops tick`` with NO ``--loop`` is a hard error pointing at the per-loop
usage, so neither a human nor an agent can start a fat fan-out tick.

Each per-loop tick first reconciles the operating mode (#2544, #61): both drivers
that fire this command — the ``t3 worker``'s deadlined subprocess timer tick
(``python -m teatree loops_tick --loop <name>``) and a manual by-hand
``t3 loops tick --loop <name>`` — converge here, so consulting
:func:`teatree.core.mode_resolution.resolve_active_mode` in ONE place reconciles both
drivers identically. When the resolved mode's ``pauses_self_pump`` is true (a
holiday-away mode only), the tick is skipped silently (parked) before any lease is
claimed or overlay is preflighted; an autonomous-away mode defers questions but does
NOT pause here, so an unattended run keeps self-pumping.

Otherwise the tick scopes the jobs builder to that ONE enabled, due row, claims
the disjoint per-loop ``loop:<name>`` lease (so the N per-loop loops run in
parallel instead of serialising on a single owner) plus the ``loop-tick:<name>``
mutex, preflights ONLY that loop's overlay (so one overlay's connector outage
cannot fail an unrelated loop's tick, LOOP-PR-C), re-anchors any deferred
self-update reinstall in this fresh subprocess before scanner imports, installs
the mini-loop schedules reader so the statusline loop line keeps its per-loop
countdowns, and runs the shared :func:`teatree.loop.tick.run_tick` pipeline (reap
+ scan + act + render).

A row with ``Loop.colleague_facing`` set is gated a second, finer-grained way
(#2904), downstream in ``teatree.loops.loop_table._loop_admitted``: it is not
admitted while ``resolved.defers_questions`` is true — ``autonomous_away``
included, even though this tick is not parked here for that mode. So an
``autonomous_away`` per-loop tick of a colleague-facing row still claims its
lease and preflights its overlay, but the jobs builder yields nothing and the
row's cadence anchor is preserved untouched (same as a disabled/not-due row).

The reactive infra loops (Slack-answer, self-improve, drain-queue) are NOT driven
here: each is its own dedicated native Claude ``/loop`` running its own
``t3 loop <slot> run`` command on its own cadence (``teatree.cli.loop*``), behind
its own dedicated ``LoopLease``.
"""

import datetime as dt
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Annotated, Any, cast

import typer
from django_typer.management import TyperCommand

from teatree.core.backend_factory import iter_overlay_backends
from teatree.core.loop_lease_manager import PER_LOOP_TICK_MUTEX_PREFIX, per_loop_owner_slot
from teatree.core.machine_output import emit
from teatree.core.mode_resolution import resolve_active_mode
from teatree.core.models import LoopLease
from teatree.loop.loop_cadences import loop_owner_ttl_seconds
from teatree.loop.preset_resolution import active_overlay_scope
from teatree.loop.statusline import set_overridden_loops_reader, set_preset_line_reader
from teatree.loops.preset_status import overridden_loop_names, preset_line_handles

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends
    from teatree.loop.job_identity import _ScannerJob
    from teatree.loop.tick import TickReport, TickRequest
    from teatree.loops.base import BuildJobsContext

type ReportDict = dict[str, Any]

#: The dedicated lease slot the reinstall drain acquires so parallel per-loop
#: ticks never both re-anchor the same pending reinstall; its short TTL doubles as
#: a throttle (a re-tick inside the window loses the CAS and skips) — the
#: CAS-as-throttle shape (claim-if-stale, never released).
_REINSTALL_DRAIN_SLOT = "loop-reinstall"
_REINSTALL_DRAIN_THROTTLE_SECONDS = 60


def _scanner_context(request: "TickRequest") -> "BuildJobsContext":
    return {
        "backends": request.backends,
        "host": request.host,
        "messaging": request.messaging,
        "notion_client": request.notion_client,
        "ready_labels": request.ready_labels,
    }


def _focus_scoped_backends() -> list["OverlayBackends"]:
    """Full-fleet overlay backends, restricted to a focus preset's ``overlay_scope`` (#3159 item 7).

    A ``focus:<overlay>`` preset carries an ``overlay_scope`` allowlist so a tick
    scans only that backend. Fail-open at every step: no active preset (or an empty
    scope) scans the whole fleet, and a scope that matches no overlay also falls
    back to the whole fleet rather than scanning nothing.
    """
    backends = iter_overlay_backends()
    try:
        scope = set(active_overlay_scope())
    except Exception:  # noqa: BLE001 — the preset layer must never blank the scan set
        return backends
    if not scope:
        return backends
    filtered = [backend for backend in backends if backend.name in scope]
    return filtered or backends


@dataclass(slots=True)
class _ScopedJobsBuilder:
    """The jobs builder scoped to the ONE loop ``t3 loops tick --loop`` fires (#2650).

    Scopes the loop-table fan-out to that single row, so the per-loop ``/loop``
    runs exactly its own loop and every other row is untouched (its cadence
    anchor unconsumed) — AND records why that row produced no jobs when the
    loop-table refused it (#3843).

    That second job is why this is an object rather than a closure. A refused
    loop and a loop that swept and found nothing both hand ``run_tick`` an empty
    job list, so without the recorded reason the command renders a control-plane
    hold as ``ran … 0 signal(s), 0 action(s)`` — the false-green that kept the
    review lane dark for hours while every tick reported success.
    """

    loop: str
    blocked_reason: str = ""

    def __call__(self, request: "TickRequest", started_at: dt.datetime) -> "list[_ScannerJob]":
        from teatree.loops.loop_table import dispatch_loop_table  # noqa: PLC0415 — deferred: lazy command import

        outcomes = dispatch_loop_table(_scanner_context(request), now=started_at, only=self.loop)
        outcome = next((candidate for candidate in outcomes if candidate.name == self.loop), None)
        if outcome is None:
            self.blocked_reason = "no mini-loop of that name is registered"
            return []
        self.blocked_reason = outcome.blocked_reason
        return list(outcome.jobs)


def _report_to_dict(report: "TickReport", *, blocked_reason: str = "") -> ReportDict:
    """The structured tick report, carrying any loop-table refusal as a real skip.

    A loop the loop-table declined to dispatch is ``skipped`` — NOT a zero-signal
    run — so a structured consumer reads the same distinction the console does.
    """
    return {
        "started_at": report.started_at.isoformat(),
        "signal_count": report.signal_count,
        "action_count": report.action_count,
        "statusline_path": str(report.statusline_path) if report.statusline_path else "",
        "errors": report.errors,
        "actions": [asdict(action) for action in report.actions],
        "skipped": bool(blocked_reason),
        "skipped_reason": blocked_reason,
    }


def _skipped_report_dict(started_at: dt.datetime, reason: str) -> ReportDict:
    """The full report contract for a tick that skipped (sibling holds the lease).

    #744 defect 1: a skipped tick must still emit every contract key (zeroed) so a
    coordinator pumping ``t3 loops tick --json`` can read ``["signal_count"]`` /
    ``["errors"]`` unconditionally — the bare ``{"skipped": ...}`` object
    ``KeyError``-ed every structured consumer on each contended beat.
    """
    return {
        "started_at": started_at.isoformat(),
        "signal_count": 0,
        "action_count": 0,
        "statusline_path": "",
        "errors": {},
        "actions": [],
        "skipped": True,
        "skipped_reason": reason,
    }


def _drain_pending_reinstall_guarded() -> None:
    """Re-anchor one deferred self-update reinstall behind a dedicated lease CAS.

    Runs in this fresh per-tick subprocess BEFORE any scanner module imports, so
    the about-to-change modules load fresh with no mixed-code window. The
    ``loop-reinstall`` lease CAS makes at most one concurrent per-loop tick drain
    (a parallel tick that loses the CAS skips), and the short TTL doubles as a
    throttle. A no-op when nothing is pending; :func:`drain_pending_reinstall`
    itself defers while a loop unit is in flight.
    """
    from teatree.loop.self_update_reinstall import drain_pending_reinstall  # noqa: PLC0415 — lazy command import

    owner = f"reinstall-{os.getpid()}-{uuid.uuid4().hex}"
    if not LoopLease.objects.acquire(
        _REINSTALL_DRAIN_SLOT, owner=owner, lease_seconds=_REINSTALL_DRAIN_THROTTLE_SECONDS
    ):
        return
    drain_pending_reinstall()


class Command(TyperCommand):
    help = "Run ONE enabled, due DB Loop by name (--loop) — the per-loop primitive each native Claude `/loop` fires."

    def _emit_skip(self, reason: str, *, json_output: bool, statusline_file: Path | None) -> None:
        from teatree.loop.tick import _write_tick_meta  # noqa: PLC0415 — deferred: keeps command import light

        now = dt.datetime.now(tz=dt.UTC)
        _write_tick_meta(now, target=statusline_file)
        emit(
            _skipped_report_dict(now, reason),
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"SKIP  {reason}",
        )

    def _build_request(self, overlay: str) -> "TickRequest":
        """The tick's scan request — ALWAYS overlay backends, never a bare host (#3843).

        ``--overlay <name>`` used to swap the full backend set for a lone
        ``CodeHostBackend``. That is not a narrower scan, it is a DIFFERENT and
        strictly poorer one: a mini-loop's ``host`` arm can only build the
        scanners a bare host supports, and the ones that need the overlay object
        — the review domain's own-PR arm among them — are silently dropped for
        the whole tick. On a solo repo that is fatal rather than merely partial:
        self-assignment as a reviewer is forbidden, so a colleague-only review
        intake makes no PR reviewable at all. Scoping the backend LIST by name
        keeps every per-overlay scanner and simply narrows which overlay runs.

        An ``--overlay`` naming no registered overlay fails loud rather than
        silently widening to the fleet or scanning nothing.
        """
        from teatree.loop.tick import TickRequest  # noqa: PLC0415 — deferred: keeps command import light

        if not overlay:
            return TickRequest(backends=_focus_scoped_backends())
        named = [backend for backend in iter_overlay_backends() if backend.name == overlay]
        if not named:
            self.stderr.write(
                f"--overlay {overlay!r} matches no registered overlay. Run `t3 info` for the registered "
                "overlays, or omit --overlay to scan the whole fleet."
            )
            raise SystemExit(2)
        return TickRequest(backends=named)

    def _emit_report(self, report: "TickReport", loop: str, *, blocked_reason: str, json_output: bool) -> None:
        """Emit the tick report, leading with a ``ran`` OR a reasoned ``SKIP`` verdict.

        A tick used to print NOTHING on the success path (``human`` was ``None``
        whenever ``errors`` was empty), so a loop that ran and found no work was
        byte-for-byte indistinguishable from one that never ran at all. Every
        tick now states its outcome (#3810), which is what made the SKIP
        starvation invisible for hours.

        #3843 closes the other half: a loop the loop-table REFUSED (force-OFF,
        held, disabled, not due, colleague-facing under an away mode) hands
        ``run_tick`` an empty job list, so it used to render as ``ran … 0
        signal(s)`` — a control-plane hold reading as a healthy quiet tick. When
        *blocked_reason* is set the verdict is a SKIP naming what refused the
        loop and how to lift it.
        """
        verdict = (
            f"SKIP  loop {loop!r} did not run — {blocked_reason}"
            if blocked_reason
            else f"ran   loop {loop!r} — {report.signal_count} signal(s), {report.action_count} action(s)"
        )
        warnings = [f"WARN  {name}: {message}" for name, message in report.errors.items()]
        emit(
            _report_to_dict(report, blocked_reason=blocked_reason),
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join([verdict, *warnings]),
        )

    def handle(
        self,
        *,
        statusline_file: Annotated[
            Path | None, typer.Option("--statusline-file", help="Override the statusline output path (test hook).")
        ] = None,
        overlay: Annotated[
            str, typer.Option("--overlay", help="Restrict scanning to the named overlay (default: all).")
        ] = "",
        loop: Annotated[
            str,
            typer.Option(
                "--loop",
                help=(
                    "REQUIRED. Run ONE enabled, due DB Loop by name (#2650) — what each native Claude "
                    "`/loop` fires. Claims the disjoint per-loop `loop:<name>` lease so the per-loop "
                    "loops run in parallel. There is no master tick: omitting --loop is a hard error."
                ),
            ),
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit the tick report as JSON.")] = False,
    ) -> None:
        if not loop.strip():
            self.stderr.write(
                "t3 loops tick requires --loop <name>. The loop is per-loop only (#2650): one "
                "self-rescheduling loop_timer chain per enabled DB Loop row that the singleton "
                "`t3 worker` drains, each firing `t3 loops tick --loop <name>` on its own cadence. "
                "There is NO master tick. Run `t3 loops list` to see the loops, then "
                "`t3 loop enable <name>` (the reconciler adds its timer); `t3 worker status` shows the worker."
            )
            raise SystemExit(2)

        # Availability reconciliation (#2544): both drivers of a per-loop tick —
        # the `t3 worker`'s deadlined subprocess timer tick and a manual by-hand
        # `t3 loops tick --loop <name>` — converge on THIS command (`python -m teatree
        # loops_tick --loop <name>` vs `t3 loops tick --loop <name>`), so gating
        # here reconciles both with zero duplicated logic. Only holiday-`away`
        # pauses the self-pump; `autonomous_away` defers questions but keeps the
        # factory self-pumping, so it must NOT park here.
        resolved = resolve_active_mode()
        if resolved.pauses_self_pump:
            self._emit_skip(
                f"mode={resolved.name} ({resolved.source}) — self-pump paused, tick parked",
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return

        # A per-loop tick (#2650) preflights ONLY its own overlay, gated on the
        # loop being enabled + due — so one overlay's connector outage can't
        # SystemExit an unrelated loop's tick (LOOP-PR-C).
        from teatree.loops.connector_preflight import (  # noqa: PLC0415 — deferred: keeps command import light
            run_loop_connector_preflight,
        )

        run_loop_connector_preflight(loop)

        from teatree.loop.driver_detection import detect_driver  # noqa: PLC0415 — deferred
        from teatree.loop.session_identity import loop_principal  # noqa: PLC0415 — deferred: keeps command import light

        owner_slot = per_loop_owner_slot(loop)
        tick_mutex = f"{PER_LOOP_TICK_MUTEX_PREFIX}{loop}"

        # ONE identity seam for every loop-ownership call site (#3810), so a
        # claim and the release that follows it can never resolve to two
        # different principals. The loop runner declares its own durable
        # principal, so its next tick re-claims the lease its previous tick took
        # instead of meeting it as a stranger; a Claude self-pump still resolves
        # its own session exactly as before.
        #
        # The lease ``owner_pid`` MUST be the durable owning process, not
        # ``os.getppid()`` of this tick subprocess (the self-pump runs it inside a
        # Bash-tool shell the harness tears down seconds later — anchoring on it
        # collapses the pid-liveness protection back to TTL-only, #1706). It is
        # the runner's own pid for a runner tick and the loop registry's durable
        # session pid for a self-pump; ``os.getppid()`` is the fallback only for a
        # direct in-session invocation with no registry record.
        session_id, principal_pid = loop_principal()
        owner_pid = principal_pid or os.getppid()
        # The per-tick re-claim IS the heartbeat, so it self-heals the driver: a
        # tick that detects a live driver registers/updates it, while a tick whose
        # detection momentarily returns blank PRESERVES the stored value
        # (``_driver_after`` preserve-on-empty) — the heartbeat can never wipe it.
        won, owner_session = LoopLease.objects.claim_ownership(
            owner_slot,
            session_id=session_id,
            ttl_seconds=loop_owner_ttl_seconds(),
            owner_pid=owner_pid,
            driver=detect_driver(session_id),
        )
        if not won:
            self._emit_skip(
                f"loop slot {owner_slot!r} not owned by this session — owner is session {owner_session} "
                f"(run `t3 loop claim --slot {owner_slot} --take-over` from the owning session to take over).",
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return

        # Re-anchor a deferred self-update reinstall before any scanner module is
        # imported, so the about-to-change modules load fresh with no mixed-code
        # window. Guarded so parallel per-loop ticks never both reinstall.
        _drain_pending_reinstall_guarded()

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire(tick_mutex, owner=owner):
            self._emit_skip(
                "another tick is already running for this loop",
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return

        from teatree.loop.statusline import set_mini_loop_schedules_reader  # noqa: PLC0415 — lazy command import
        from teatree.loop.tick import run_tick  # noqa: PLC0415 — deferred: keeps command import light
        from teatree.loops.schedule import mini_loop_schedules  # noqa: PLC0415 — deferred: keeps command import light

        # The statusline dedicated loop line shows due-soon loops with their own
        # next-tick countdowns (#1400) under the active-preset/schedule handle
        # (#3159/#3248); install the live DB-backed readers so this per-loop tick's
        # render carries that handle AND collapses the routine per-loop leases into
        # it (the overridden-loops reader is what activates the collapse — so it is
        # installed together with the preset segment that represents the folded
        # loops), then reset after the tick so the process-global seams never leak.
        set_mini_loop_schedules_reader(mini_loop_schedules)
        set_preset_line_reader(preset_line_handles)
        set_overridden_loops_reader(overridden_loop_names)
        builder = _ScopedJobsBuilder(loop=loop)
        try:
            request = self._build_request(overlay)
            report = run_tick(request, statusline_path=statusline_file, jobs_builder=builder)
        finally:
            set_mini_loop_schedules_reader(None)
            set_preset_line_reader(None)
            set_overridden_loops_reader(None)
            LoopLease.objects.release(tick_mutex, owner=owner)

        self._emit_report(report, loop, blocked_reason=builder.blocked_reason, json_output=json_output)
        self._hard_exit_if_subprocess()

    @staticmethod
    def _hard_exit_if_subprocess() -> None:
        """``os._exit`` right after render when this is the worker's deadlined subprocess.

        A hung NON-daemon scanner thread blocks interpreter shutdown (the
        ``ThreadPoolExecutor`` atexit join it left running), pinning this subprocess —
        and one of the worker's scarce ``loops`` executor slots — until the outer
        deadline SIGKILL. Once the report is rendered there is nothing left to do, so a
        hard exit reclaims the slot immediately. Gated on the env marker the worker's
        ``run_deadlined_tick`` sets, so an in-process ``call_command`` (tests) NEVER
        hits it. Streams are flushed first because ``os._exit`` skips atexit flushing.
        """
        from teatree.loops.deadlined_tick import (  # noqa: PLC0415 — deferred: keep import light
            TICK_SUBPROCESS_ENV_MARKER,
        )

        if not os.environ.get(TICK_SUBPROCESS_ENV_MARKER):
            return
        import sys  # noqa: PLC0415 — deferred: only needed on the subprocess exit path

        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

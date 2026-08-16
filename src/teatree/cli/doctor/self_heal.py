"""Self-heal doctor checks — the H24 factory-outage detectors (owner directive #10).

The worker WAS the monitor, so when it died the alerting died with it (the
recorded seven-hour silent freeze). These crash-proof ``_check_*`` detectors
surface the silent-failure classes as loud findings so the in-daemon watchdog
(the ``deploy/watchdog.sh`` sidecar, kept alive by the Docker daemon independently
of the stack it watches) can restart the stack and DM the owner. Each says REPAIRS or REPORTS (#4359):

- a compose init container that exited non-zero, or any long-running service —
    worker, admin, or the watchdog itself — stuck ``Created``/``Exited`` (REPORTS),
- a free worker flock while the loop machinery has queued, overdue work (REPORTS),
- an ``execute_task`` claimed RUNNING with no live worker to finish it (REPORTS),
- a READY loop timer stale past 2x its cadence (a wedged drain) (REPORTS),
- a still-live ticket whose NEWEST task FAILED with no successor — the freeze signature (REPORTS),
- a runtime clone that has drifted off its default branch (REPORTS),
- a ``worker_quiescing`` gate outliving any deploy that could explain it (REPAIRS when provably dead, else REPORTS),
- a slack-drain sidecar failing every pass or gone silent (``self_heal_slack_drain`` — REPORTS),
- a Slack app-config token pair aging toward its 12-hour expiry, past which it is
    unrecoverable (``self_heal_slack_config_token`` — REPAIRS, it auto-rotates),
- a ``loop:<name>``/``t3-master`` lease held by a dead session past TTL (REPAIRS).

Each returns ``bool`` — ``False`` is a hard FAIL that reddens ``t3 doctor`` (and so
the watchdog's ``t3 doctor --json``). Every check is crash-proof: any error degrades
to a pass, since a detector that aborted the run would recreate the very "monitor
dies, alerting dies" failure this module ends.
"""

import base64
import datetime as dt
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import typer

from teatree.cli.doctor.self_heal_quiescing import check_stranded_quiescing_gate
from teatree.cli.doctor.self_heal_slack_config_token import check_slack_config_token_fresh
from teatree.cli.doctor.self_heal_slack_drain import check_slack_drain_alive

#: The compose project the box runs the factory under (``deploy/docker-compose.yml``).
_COMPOSE_PROJECT = "teatree"
#: Env var the socket-holding watchdog uses to hand compose container states to
#: ``t3 doctor`` (base64 of the ``docker ps`` tab-rows); see
#: :func:`_compose_states_from_handoff`.
_COMPOSE_STATES_ENV = "TEATREE_DOCTOR_COMPOSE_PS"
#: The one-shot prep service — a non-zero exit is a crash-looping init.
_INIT_SERVICE = "teatree-init"
#: The long-running services expected to stay ``running`` while loops are enabled.
#: ``teatree-watchdog`` belongs here precisely because it is the supervisor: it is
#: the one container nothing else restarts, so a watchdog stuck ``Created`` (an
#: unmountable bind source, say) removes the alerting for every other failure in
#: this module and does it silently. Omitting it left that blind spot unreported.
_LONG_RUNNING_SERVICES = ("teatree-worker", "teatree-admin", "teatree-watchdog")
#: A container state that means a long-running service is NOT serving.
_DOWN_STATES = frozenset({"created", "exited", "dead", "restarting", "paused"})
#: The tab-separated ``service\tstate\tstatus`` fields the docker probe emits.
_STATE_ROW_FIELDS = 3

#: A READY loop timer older than this multiple of its cadence is a stalled drain.
_STALE_TIMER_CADENCE_MULTIPLIER = 2
#: Floor so a fast (e.g. 60s) loop's timer does not flap on ordinary tick jitter.
_MIN_STALE_TIMER_SECONDS = 300
#: A loop with no interval/daily cadence falls back to this nominal cadence.
_DEFAULT_CADENCE_SECONDS = 300
#: Env vars ``deploy/docker-compose.yml`` forwards into every service naming the box's
#: runtime clone, most specific first. Hard-coding the box default instead left the
#: drift detector silently inert wherever the deployment put its clone elsewhere.
_RUNTIME_CLONE_ENVS = ("TEATREE_CLONE_DIR", "TEATREE_DEPLOY_CHECKOUT")
#: Where the box has historically kept it, for a venue that declares neither.
_BOX_RUNTIME_CLONE = Path("/home/teatree/teatree")


def _parse_compose_state_rows(text: str) -> list[tuple[str, str, str]]:
    """Parse tab-separated ``docker ps`` lines into ``(service, state, status)`` tuples.

    Each line carries three tab-separated fields -- service, state, status (the
    probe's ``--format``); the state is lower-cased for the down-state comparison.
    Malformed / short lines are dropped so a partial read never yields a garbage
    verdict.
    """
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == _STATE_ROW_FIELDS and parts[0]:
            rows.append((parts[0], parts[1].strip().lower(), parts[2].strip()))
    return rows


def _compose_states_from_handoff() -> list[tuple[str, str, str]] | None:
    """Compose states handed off by the socket-holding watchdog, or ``None`` when absent.

    ``t3 doctor`` runs inside an app container (docker CLI but no
    ``/var/run/docker.sock``), so only the socket-holding watchdog can gather the
    states; it passes them in via :data:`_COMPOSE_STATES_ENV` (base64 of the
    tab-separated ``docker ps`` output). ``None`` when the env var is unset/empty
    (caller falls back to a LOCAL ``docker ps``) or the handoff is malformed
    (degrade to a pass, never a garbage verdict).
    """
    raw = os.environ.get(_COMPOSE_STATES_ENV, "").strip()
    if not raw:
        return None
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return _parse_compose_state_rows(decoded)


class _Probe:
    """Crash-tolerant reads of the loop/worker/clone state the checks aggregate.

    Grouped as static methods so the module stays under the module-health
    public-function cap while each detector stays a thin, single-concern
    ``_check_*`` wrapper.
    """

    @staticmethod
    def loop_runner_on() -> bool:
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light

        return get_effective_settings().loop_runner_enabled

    @staticmethod
    def worker_flock_free() -> bool:
        from teatree.utils.singleton import WORKER_SINGLETON, flock_is_held  # noqa: PLC0415 — deferred: light import

        return not flock_is_held(WORKER_SINGLETON)

    @staticmethod
    def compose_container_states(project: str) -> list[tuple[str, str, str]] | None:
        """``(service, state, status)`` per container of *project*, or ``None`` when unreadable.

        Prefers the watchdog handoff (:func:`_compose_states_from_handoff`), since the
        doctor's socket-less app container cannot reach the daemon; falls back to a
        LOCAL ``docker ps`` when no handoff is present (a dev box). ``None`` means
        "cannot tell" (no handoff, no ``docker`` on PATH / daemon down / timeout) — the
        caller degrades to a pass, as the MCP / Slack probes do when their tool is absent.
        """
        from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415 — deferred: keeps CLI startup light

        handoff = _compose_states_from_handoff()
        if handoff is not None:
            return handoff
        docker = shutil.which("docker")
        if docker is None:
            return None
        try:
            completed = run_allowed_to_fail(
                [
                    docker,
                    "ps",
                    "--all",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    "--format",
                    '{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}',
                ],
                expected_codes=None,
                timeout=15,
            )
        except (OSError, ValueError):
            return None
        if completed.returncode != 0:
            return None
        return _parse_compose_state_rows(completed.stdout)

    @staticmethod
    def _cadence_seconds(loop_row: object) -> int:
        delay = getattr(loop_row, "delay_seconds", None)
        if isinstance(delay, int) and delay > 0:
            return delay
        return _DEFAULT_CADENCE_SECONDS

    @staticmethod
    def overdue_ready_timers(now: dt.datetime) -> list[tuple[str, dt.datetime, int]]:
        """``(loop_name, run_after, threshold_seconds)`` for READY timers overdue past 2x cadence."""
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep

        from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry
        from teatree.loops.timer_chains import _loop_timer_path  # noqa: PLC0415 — deferred: loaded at call time

        loops = {row.name: row for row in Loop.objects.all()}
        overdue: list[tuple[str, dt.datetime, int]] = []
        rows = DBTaskResult.objects.filter(task_path=_loop_timer_path(), status=TaskResultStatus.READY)
        for row in rows:
            args = row.args_kwargs.get("args") or []
            if not args:
                continue
            name = args[0]
            cadence = _Probe._cadence_seconds(loops.get(name))
            threshold = max(_STALE_TIMER_CADENCE_MULTIPLIER * cadence, _MIN_STALE_TIMER_SECONDS)
            if row.run_after is not None and row.run_after < now - dt.timedelta(seconds=threshold):
                overdue.append((name, row.run_after, threshold))
        return overdue

    @staticmethod
    def stranded_runner_results(now: dt.datetime) -> list[tuple[str, dt.datetime]]:
        """``(job_id, started_at)`` for ``execute_task`` RUNNING past the stranded grace."""
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep

        from teatree.core.tasks import (  # noqa: PLC0415 — deferred: task import needs the registry
            STRANDED_JOB_GRACE_SECONDS,
            execute_task,
        )

        cutoff = now - dt.timedelta(seconds=STRANDED_JOB_GRACE_SECONDS)
        rows = DBTaskResult.objects.filter(
            task_path=execute_task.module_path,
            status=TaskResultStatus.RUNNING,
        )
        return [(str(row.id), row.started_at) for row in rows if row.started_at is not None and row.started_at < cutoff]

    @staticmethod
    def declared_runtime_clones() -> list[Path]:
        """Every runtime-clone path this venue's deployment declares, most specific first.

        Empty on a venue that declares none (any dev machine) — which is what tells a
        misconfigured deployment from a laptop that simply has no H24 clone.
        """
        declared = (os.environ.get(name, "").strip() for name in _RUNTIME_CLONE_ENVS)
        return [Path(value) for value in declared if value]

    @staticmethod
    def runtime_clone_root() -> Path | None:
        """The box's long-lived runtime clone if present as a git checkout, else ``None``.

        Scoped to what the DEPLOYMENT declares (:func:`declared_runtime_clones`, falling
        back to the historical box path) — deliberately NOT the running code's repo root,
        so this invariant fires only for the H24 factory's own runtime clone and never
        for a legitimate feature-branch worktree a developer runs ``t3`` from. A venue
        with no such clone (any dev machine) resolves to ``None`` and the check degrades
        to a pass.
        """
        candidates = [*_Probe.declared_runtime_clones(), _BOX_RUNTIME_CLONE]
        return next((root for root in candidates if (root / ".git").exists()), None)

    @staticmethod
    def parse_findings(text: str) -> list[dict[str, str]]:
        """Split doctor echo lines into ``{"level", "message", "identity"}`` records for ``--json``.

        The doctor convention prefixes every line with its level token
        (``FAIL`` / ``WARN`` / ``OK``); a line without a recognised token is
        carried as an ``INFO`` record so nothing is dropped.

        ``identity`` is the volatility-normalized form the watchdog keys its owner DM on
        (:func:`~teatree.cli.doctor.finding_digest.finding_identity`), so an unchanged
        condition whose message carries a ticking counter re-pages nobody.
        """
        from teatree.cli.doctor.finding_digest import finding_identity  # noqa: PLC0415 — deferred: --json path only

        findings: list[dict[str, str]] = []
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            token = line.split(maxsplit=1)[0]
            level = token if token in {"FAIL", "WARN", "OK"} else "INFO"
            message = line[len(token) :].strip() if level != "INFO" else line.strip()
            findings.append({"level": level, "message": message, "identity": finding_identity(message)})
        return findings


def _check_compose_stack() -> bool:
    """FAIL when the compose init crash-loops or a long-running service is down.

    A non-zero init exit is a crash-looping prep container (nothing downstream
    starts); a worker/admin container stuck ``Created``/``Exited`` while
    ``loop_runner_enabled`` is ON is a silently dead factory. The container states
    come from the watchdog handoff (:func:`_compose_states_from_handoff`) since the
    doctor runs in a socket-less app container; only when neither the handoff nor a
    local ``docker ps`` can read the daemon (a dev box) does the probe return
    ``None`` and this degrade to a pass — the watchdog's own ``docker compose up
    -d`` is the real container-restart repair.
    """
    try:
        states = _Probe.compose_container_states(_COMPOSE_PROJECT)
        if states is None:
            return True
        runner_on = _Probe.loop_runner_on()
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Compose-stack check crashed: {exc.__class__.__name__}: {exc}")
        return True

    ok = True
    for service, state, status in states:
        if service == _INIT_SERVICE and state == "exited" and "(0)" not in status:
            typer.echo(
                f"FAIL  Compose init container {service} exited non-zero ({status}) — the prep "
                f"container is crash-looping, so the worker/admin never start. Inspect "
                f"`docker compose -p {_COMPOSE_PROJECT} logs {service}` and restart: "
                f"`docker compose -p {_COMPOSE_PROJECT} up -d`."
            )
            ok = False
        elif service in _LONG_RUNNING_SERVICES and runner_on and state in _DOWN_STATES:
            typer.echo(
                f"FAIL  Compose service {service} is {state} ({status}) while loop_runner_enabled is "
                f"ON — the factory is silently down. Restart it: "
                f"`docker compose -p {_COMPOSE_PROJECT} up -d`."
            )
            ok = False
    return ok


def _check_loop_worker_alive() -> bool:
    """FAIL when the worker flock is free while overdue loop work is queued.

    The keystone silent-freeze signature: ``loop_runner_enabled`` is ON, no
    process holds the worker flock, AND at least one READY loop timer is overdue
    past 2x its cadence — so queued loop work exists that nothing is draining.
    Gating on the overdue-timer evidence (not the bare free flock, which the
    softer ``_check_worker_running`` WARN already surfaces) keeps a dev box that
    simply has no worker running from reddening, while the box's genuinely dead
    worker is caught loudly.
    """
    try:
        if not (_Probe.loop_runner_on() and _Probe.worker_flock_free()):
            return True
        overdue = _Probe.overdue_ready_timers(_now())
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Loop-worker-alive check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not overdue:
        return True
    names = ", ".join(sorted({name for name, _, _ in overdue}))
    typer.echo(
        f"FAIL  No worker holds the loop flock but {len(overdue)} loop timer(s) are overdue "
        f"({names}) — the loops are silently dead. Start the worker: `t3 worker ensure` "
        f"(on the box: `docker compose -p {_COMPOSE_PROJECT} up -d teatree-worker`)."
    )
    return False


def _check_stranded_task() -> bool:
    """FAIL when an ``execute_task`` is RUNNING past its grace with no live worker.

    A headless task claimed RUNNING whose executor died leaves the row RUNNING
    forever — nothing will ever finish it, and the ticket silently freezes. When
    the worker flock is also free there is provably no live worker to complete
    it; the started-at grace absorbs a brief worker restart.
    """
    try:
        if not _Probe.worker_flock_free():
            return True
        stranded = _Probe.stranded_runner_results(_now())
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Stranded-headless-task check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not stranded:
        return True
    ids = ", ".join(job_id for job_id, _ in stranded)
    typer.echo(
        f"FAIL  {len(stranded)} execute_task job(s) are RUNNING with no live worker to "
        f"finish them ({ids}) — the claiming executor died mid-run and the ticket is frozen. "
        f"Restart the worker (`t3 worker ensure`); the reconciler re-heads the chain."
    )
    return False


def _check_stale_loop_timer() -> bool:
    """FAIL when a READY loop timer is older than 2x its cadence (a wedged drain).

    Worker-agnostic: catches a worker that holds the flock but is wedged (its
    timers pile up unconsumed) as well as one that is down. A READY loop timer
    whose ``run_after`` predates ``now - 2 x cadence`` should already have fired.
    """
    try:
        overdue = _Probe.overdue_ready_timers(_now())
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Stale-loop-timer check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not overdue:
        return True
    # Collapse to ONE FAIL summary keyed on the SET of overdue timers (sorted
    # names, no timestamps): the watchdog digests FAIL findings into its DM key, so
    # a volatile ``run_after`` in the summary would re-DM the whole bundle every pass
    # even when the set is unchanged (#slack-comms). The per-timer ``run_after``
    # detail goes on non-FAIL lines — visible in ``t3 doctor``, excluded from the
    # dedup body.
    names = sorted(name for name, _run_after, _threshold in overdue)
    typer.echo(
        f"FAIL  {len(names)} loop timer(s) READY but overdue past 2x cadence: "
        f"{', '.join(names)}. The drain is stalled; check the worker "
        f"(`t3 worker ensure` / worker logs)."
    )
    for name, run_after, threshold in sorted(overdue):
        typer.echo(
            f"INFO    {name}: due {run_after.isoformat()}, past 2x its "
            f"{threshold // _STALE_TIMER_CADENCE_MULTIPLIER}s cadence."
        )
    return False


def _check_failed_tasks_on_live_tickets() -> bool:
    """FAIL when a live ticket's NEWEST task is FAILED — work that died with no successor.

    The decision rule (souliane/teatree#4357): a non-terminal ticket whose newest task
    FAILED and which nothing re-dispatched is either re-dispatched or explicitly parked,
    never left in neither state. A ticket that DOES carry a successor is being advanced,
    so naming it says nothing an operator can act on — and a line of dozens of
    unactionable names is how a real freeze becomes invisible among them.

    Newest is by ``pk``: an ``AutoField`` is monotonic and never null, where
    ``Task.created_at`` is nullable and would order legacy rows arbitrarily.

    Synthetic rows are excluded (souliane/teatree#3492). A
    ``<scheme>://<overlay>`` loop-cadence anchor is a recurring schedule with no
    terminal state to reach, and a bare-number ``issue_url`` is malformed debris
    whose real, terminal ticket exists separately. Neither is frozen deliverable
    work, yet both render as forge-looking numbers and pin a permanent,
    unactionable FAIL. A ticket with no forge issue at all (``""`` /
    ``auto:<branch>``) is ordinary work and still counts.
    """
    try:
        from django.db.models import OuterRef, Subquery  # noqa: PLC0415 — deferred: ORM import needs the app registry

        from teatree.core.forge_url import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
            is_synthetic_ticket_url,
        )
        from teatree.core.models import Task, Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

        terminal = set(Ticket._TERMINAL_STATES) | {Ticket.State.RETROSPECTED}  # noqa: SLF001 — the model's SSOT terminal set
        newest_status = Subquery(Task.objects.filter(ticket=OuterRef("pk")).order_by("-pk").values("status")[:1])
        frozen = (
            Ticket.objects.exclude(state__in=terminal)
            .annotate(newest_task_status=newest_status)
            .filter(newest_task_status=Task.Status.FAILED)
        )
        numbers = sorted({ticket.ticket_number for ticket in frozen if not is_synthetic_ticket_url(ticket.issue_url)})
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Failed-task-on-live-ticket check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not numbers:
        return True
    listed = ", ".join(f"#{number}" for number in numbers)
    typer.echo(
        f"FAIL  The newest task FAILED with no successor on {len(numbers)} non-terminal ticket(s) "
        f"({listed}) — the silent-freeze signature: work died and nothing is advancing the ticket. "
        f"Re-dispatch each, or park it with a reason."
    )
    return False


def _check_runtime_clone_on_default_branch() -> bool:
    """FAIL when the runtime clone has drifted off its default branch.

    The box's ``t3 worker`` imports teatree from a long-lived clone that must
    track the default branch; a stray checkout (or a self-update left mid-flight)
    leaves the loop running stale/wrong code with no error. Best-effort: a
    non-git or unresolvable clone degrades to a pass — but a DECLARED clone that
    cannot be resolved is WARNed rather than passed over in silence, since an
    inert detector reads exactly like a healthy one (#4339).
    """
    from teatree.utils import git  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        root = _Probe.runtime_clone_root()
        if root is None:
            declared = _Probe.declared_runtime_clones()
            if declared:
                typer.echo(
                    f"WARN  No declared runtime clone is a git checkout "
                    f"({', '.join(str(path) for path in declared)}) — the drift detector cannot "
                    f"run, so a clone left on a stray branch would go unreported. Point "
                    f"{_RUNTIME_CLONE_ENVS[0]} at the box's clone, or mount it there."
                )
            return True
        current = git.current_branch(repo=str(root))
        default = git.default_branch(repo=str(root))
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Runtime-clone-branch check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not default or not current or current == default:
        return True
    where = git.DETACHED_HEAD if current == git.DETACHED_HEAD else f"branch {current!r}"
    typer.echo(
        f"FAIL  Runtime clone {root} is on {where}, not the default branch {default!r} — the loop "
        f"is running drifted code. Restore it: `git -C {root} checkout {default} && git -C {root} pull`."
    )
    return False


def _check_dead_owner_lease() -> bool:
    """AUTO-REPAIR (not just FAIL) a loop lease held by a dead session past TTL (#3571).

    Reclaims every ``loop:<name>``/``t3-master`` lease whose owning session is provably
    dead (crashed, or a reused / cross-namespace pid) and reports the heal. Conservative,
    idempotent, best-effort — any error degrades to a pass rather than aborting the run.
    """
    try:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

        reclaimed = LoopLease.objects.reclaim_dead_owner_leases()
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Dead-owner-lease check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if reclaimed:
        typer.echo(
            f"WARN  Auto-reclaimed {len(reclaimed)} loop lease(s) held by a dead session past TTL "
            f"({', '.join(sorted(reclaimed))}) — returned to the pool for a live worker to drive."
        )
    return True


def _now() -> dt.datetime:
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    return timezone.now()


def run_self_heal_checks() -> bool:
    """Run every self-heal detector; return ``False`` if any hard FAILs.

    The single entry point ``t3 doctor`` wires into its check sequence, so the
    silent-freeze classes flip the doctor exit code the watchdog container keys on.
    """
    checks: tuple[Callable[[], bool], ...] = (
        _check_compose_stack,
        _check_loop_worker_alive,
        _check_stranded_task,
        _check_stale_loop_timer,
        _check_failed_tasks_on_live_tickets,
        _check_runtime_clone_on_default_branch,
        check_stranded_quiescing_gate,
        check_slack_drain_alive,
        check_slack_config_token_fresh,
        _check_dead_owner_lease,
    )
    ok = True
    for check in checks:
        ok = check() and ok
    return ok


def check_as_json(run_checks: Callable[[], bool]) -> bool:
    """Run *run_checks* capturing its echoes and emit ``{"ok", "findings"}`` JSON.

    The ``t3 doctor --json`` surface the watchdog container consumes: it inspects
    ``ok`` for the exit verdict and ``findings`` (level-tagged) for the DM body.
    *run_checks* is a zero-arg callable that already carries the resolved
    ``repair`` value, so the JSON path never re-invokes with repair implicitly
    enabled (#3313).
    """
    import contextlib  # noqa: PLC0415 — deferred: loaded only on the --json path
    import io  # noqa: PLC0415 — deferred: loaded only on the --json path

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        ok = run_checks()
    typer.echo(json.dumps({"ok": ok, "findings": _Probe.parse_findings(buffer.getvalue())}))
    return ok

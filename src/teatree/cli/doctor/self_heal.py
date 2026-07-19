"""Self-heal doctor checks — the H24 factory-outage detectors (owner directive #10).

The recorded 2026 seven-hour silent freeze: the worker WAS the monitor, so when
the worker/init container died the thing that would alert died with it, and
NOBODY noticed. These crash-proof ``_check_*`` detectors surface the
silent-failure classes that froze the factory as loud red findings so the
in-daemon watchdog (the ``deploy/watchdog.sh`` sidecar container, kept alive by
the Docker daemon independently of the stack it watches) can restart the stack
and DM the owner:

- a compose init container that exited non-zero / a worker stuck ``Created``,
- a free worker flock while the loop machinery has queued, overdue work,
- an ``execute_headless_task`` claimed RUNNING with no live worker to finish it,
- a READY loop timer stale past 2x its cadence (a wedged drain),
- a PENDING ``interactive`` task under ``agent_runtime=headless`` (unrunnable),
- a FAILED task on a still-live ticket (the silent-freeze signature),
- a runtime clone that has drifted off its default branch,
- a slack-drain sidecar failing every pass or gone silent, so inbound Slack stops being answered.

Each returns ``bool`` — ``False`` is a hard FAIL that reddens ``t3 doctor`` (and
so the watchdog's ``t3 doctor --json``). Every check is crash-proof: any
unexpected error degrades to a pass, because a self-heal detector that itself
aborts the doctor run would recreate the very "monitor dies, alerting dies"
failure this module exists to end.
"""

import base64
import datetime as dt
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import typer

from teatree.paths import DATA_DIR

#: The compose project the box runs the factory under (``deploy/docker-compose.yml``).
_COMPOSE_PROJECT = "teatree"
#: Env var the socket-holding watchdog uses to hand compose container states to
#: ``t3 doctor`` (base64 of the ``docker ps`` tab-rows); see
#: :func:`_compose_states_from_handoff`.
_COMPOSE_STATES_ENV = "TEATREE_DOCTOR_COMPOSE_PS"
#: The one-shot prep service — a non-zero exit is a crash-looping init.
_INIT_SERVICE = "teatree-init"
#: The long-running services expected to stay ``running`` while loops are enabled.
_LONG_RUNNING_SERVICES = ("teatree-worker", "teatree-admin")
#: A container state that means a long-running service is NOT serving.
_DOWN_STATES = frozenset({"created", "exited", "dead", "restarting", "paused"})
#: The tab-separated ``service\tstate\tstatus`` fields the docker probe emits.
_STATE_ROW_FIELDS = 3

#: The slack-drain sidecar heartbeat filename under :data:`teatree.paths.DATA_DIR`
#: (the shared data bind mount). ``deploy/entrypoint.sh``'s ``slack_drain_loop``
#: rewrites it every pass; doctor — running in another container — reads it here.
#: The filename is pinned to the entrypoint by ``tests/test_deploy_slack_listener.py``.
_SLACK_DRAIN_HEARTBEAT_FILENAME = "slack-drain-heartbeat.json"
#: A drain that has failed this many passes in a row is a real, non-transient break.
_MAX_DRAIN_CONSECUTIVE_FAILURES = 5
#: The heartbeat must refresh within max(this x its interval, floor) or the sidecar
#: is dead/wedged. The multiplier absorbs a slow pass; the floor covers a fast cadence.
_DRAIN_HEARTBEAT_STALE_MULTIPLIER = 4
_DRAIN_HEARTBEAT_STALE_FLOOR_SECONDS = 120


@dataclass(frozen=True, slots=True)
class DrainBeat:
    """One parsed slack-drain heartbeat: when it last ran and its failure streak."""

    updated_at: dt.datetime
    consecutive_failures: int
    interval_seconds: int


#: A READY loop timer older than this multiple of its cadence is a stalled drain.
_STALE_TIMER_CADENCE_MULTIPLIER = 2
#: Floor so a fast (e.g. 60s) loop's timer does not flap on ordinary tick jitter.
_MIN_STALE_TIMER_SECONDS = 300
#: A loop with no interval/daily cadence falls back to this nominal cadence.
_DEFAULT_CADENCE_SECONDS = 300
#: Grace before a RUNNING headless task with no live worker is deemed stranded —
#: absorbs a brief worker restart that momentarily frees the flock mid-claim.
_STRANDED_HEADLESS_GRACE_SECONDS = 900
#: The box's runtime clone; used as a fallback when the running code's repo root
#: cannot be resolved (``deploy/docker-compose.yml`` mounts the clone here).
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

    ``t3 doctor`` runs inside ``teatree-admin``, which has the ``docker`` CLI but
    NOT ``/var/run/docker.sock`` (only the watchdog mounts the socket), so a local
    ``docker ps`` there cannot reach the daemon and the compose-stack detector would
    silently pass every real outage. The watchdog — the ONE container with the
    socket — gathers the states and passes them in via :data:`_COMPOSE_STATES_ENV`
    (base64 of the same tab-separated ``docker ps`` output), so the detector runs in
    the container that also has the DB-backed ``loop_runner_on`` gate.

    ``None`` when the env var is unset / empty (a dev box, or a direct ``t3 doctor``
    run) so the caller falls back to a LOCAL ``docker ps``. A malformed / non-base64
    handoff also yields ``None`` (degrade to a pass) rather than a garbage verdict.
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

        Prefers the watchdog handoff (:func:`_compose_states_from_handoff`): the
        doctor runs inside a socket-LESS app container (``teatree-admin`` has the
        ``docker`` CLI but not ``/var/run/docker.sock``), so a local ``docker ps``
        cannot reach the daemon there and the detector would silently pass every
        real outage. The socket-holding watchdog gathers the states and hands them
        in, so the check runs where the DB-backed ``loop_runner_on`` gate lives.

        Falls back to a LOCAL ``docker ps`` when no handoff is present (a dev box,
        or a direct ``t3 doctor`` run on a machine WITH docker access). ``None``
        means "cannot tell" (no handoff, and no ``docker`` on PATH / daemon down /
        timeout) — the caller degrades to a pass, exactly as the MCP / Slack doctor
        probes degrade when their tool is absent.
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
    def stranded_headless_results(now: dt.datetime) -> list[tuple[str, dt.datetime]]:
        """``(job_id, started_at)`` for ``execute_headless_task`` RUNNING past the stranded grace."""
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep

        from teatree.core.tasks import execute_headless_task  # noqa: PLC0415 — deferred: task import needs the registry

        cutoff = now - dt.timedelta(seconds=_STRANDED_HEADLESS_GRACE_SECONDS)
        rows = DBTaskResult.objects.filter(
            task_path=execute_headless_task.module_path,
            status=TaskResultStatus.RUNNING,
        )
        return [(str(row.id), row.started_at) for row in rows if row.started_at is not None and row.started_at < cutoff]

    @staticmethod
    def runtime_clone_root() -> Path | None:
        """The box's long-lived runtime clone if present as a git checkout, else ``None``.

        Scoped to the fixed box mount (``deploy/docker-compose.yml`` mounts the
        clone there) — deliberately NOT the running code's repo root, so this
        invariant fires only for the H24 factory's own runtime clone and never
        for a legitimate feature-branch worktree a developer runs ``t3`` from.
        A box without that clone (any dev machine) resolves to ``None`` and the
        check degrades to a pass.
        """
        return _BOX_RUNTIME_CLONE if (_BOX_RUNTIME_CLONE / ".git").exists() else None

    @staticmethod
    def parse_findings(text: str) -> list[dict[str, str]]:
        """Split doctor echo lines into ``{"level", "message"}`` records for ``--json``.

        The doctor convention prefixes every line with its level token
        (``FAIL`` / ``WARN`` / ``OK``); a line without a recognised token is
        carried as an ``INFO`` record so nothing is dropped.
        """
        findings: list[dict[str, str]] = []
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            token = line.split(maxsplit=1)[0]
            level = token if token in {"FAIL", "WARN", "OK"} else "INFO"
            message = line[len(token) :].strip() if level != "INFO" else line.strip()
            findings.append({"level": level, "message": message})
        return findings

    @staticmethod
    def slack_drain_heartbeat() -> "DrainBeat | None":
        """The slack-drain sidecar's last heartbeat, or ``None`` when absent/unreadable.

        ``None`` means the box runs no slack-drain sidecar (a dev machine, or a
        deploy without the listener) OR the file is unparsable — the caller
        degrades to a pass, never a false FAIL. Read from
        :data:`teatree.paths.DATA_DIR` so a test can repoint the whole probe by
        patching that name on this module.
        """
        path = DATA_DIR / _SLACK_DRAIN_HEARTBEAT_FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            updated = dt.datetime.fromtimestamp(int(raw["updated_at"]), tz=dt.UTC)
            return DrainBeat(
                updated_at=updated,
                consecutive_failures=int(raw["consecutive_failures"]),
                interval_seconds=int(raw.get("interval_seconds", 0)),
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None


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


def _check_stranded_headless_task() -> bool:
    """FAIL when an ``execute_headless_task`` is RUNNING past its grace with no live worker.

    A headless task claimed RUNNING whose executor died leaves the row RUNNING
    forever — nothing will ever finish it, and the ticket silently freezes. When
    the worker flock is also free there is provably no live worker to complete
    it; the started-at grace absorbs a brief worker restart.
    """
    try:
        if not _Probe.worker_flock_free():
            return True
        stranded = _Probe.stranded_headless_results(_now())
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Stranded-headless-task check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not stranded:
        return True
    ids = ", ".join(job_id for job_id, _ in stranded)
    typer.echo(
        f"FAIL  {len(stranded)} execute_headless_task job(s) are RUNNING with no live worker to "
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
    # names, no timestamps): the watchdog RED body-hash keys on FAIL messages, so
    # a volatile ``run_after`` in the summary would churn the hash and re-DM the
    # whole bundle every pass even when the set is unchanged (#slack-comms). The
    # per-timer ``run_after`` detail goes on non-FAIL lines — visible in
    # ``t3 doctor``, excluded from the dedup body.
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


def _check_interactive_task_under_headless() -> bool:
    """FAIL when PENDING ``interactive`` tasks exist under ``agent_runtime=headless``.

    In headless runtime only the headless lane drains the queue, so a PENDING
    task pinned ``execution_target=interactive`` can never be claimed — it stalls
    forever with no error. Surfaces the count so the operator can re-target or
    flip the runtime.
    """
    try:
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light
        from teatree.config.agent_enums import AgentRuntime  # noqa: PLC0415 — deferred: keeps CLI startup light
        from teatree.core.models import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry

        if get_effective_settings().agent_runtime is not AgentRuntime.HEADLESS:
            return True
        stalled = Task.objects.filter(
            status=Task.Status.PENDING,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        ).count()
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Interactive-under-headless check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not stalled:
        return True
    typer.echo(
        f"FAIL  {stalled} PENDING task(s) are pinned execution_target=interactive under "
        f"agent_runtime=headless — the headless lane cannot claim them, so they stall silently. "
        f"Re-target them or run the interactive loop lane."
    )
    return False


def _check_failed_tasks_on_live_tickets() -> bool:
    """FAIL when FAILED tasks sit on still-live (non-terminal) tickets — the freeze signature.

    A FAILED task whose ticket has not reached a terminal/retrospected state is
    work that died with nothing advancing the ticket: the silent-freeze pattern
    the incident exhibited. Reports the count and the affected ticket numbers.
    """
    try:
        from teatree.core.models import Task, Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

        terminal = set(Ticket._TERMINAL_STATES) | {Ticket.State.RETROSPECTED}  # noqa: SLF001 — the model's SSOT terminal set
        frozen = (
            Task.objects.filter(status=Task.Status.FAILED).exclude(ticket__state__in=terminal).select_related("ticket")
        )
        numbers = sorted({task.ticket.ticket_number for task in frozen})
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Failed-task-on-live-ticket check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not numbers:
        return True
    listed = ", ".join(f"#{number}" for number in numbers)
    typer.echo(
        f"FAIL  FAILED task(s) sit on {len(numbers)} non-terminal ticket(s) ({listed}) — the "
        f"silent-freeze signature: work died and nothing is advancing the ticket. Inspect the "
        f"failed attempts and re-dispatch or close the tickets."
    )
    return False


def _check_runtime_clone_on_default_branch() -> bool:
    """FAIL when the runtime clone has drifted off its default branch.

    The box's ``t3 worker`` imports teatree from a long-lived clone that must
    track the default branch; a stray checkout (or a self-update left mid-flight)
    leaves the loop running stale/wrong code with no error. Best-effort: a
    non-git or unresolvable clone degrades to a pass.
    """
    from teatree.utils import git  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        root = _Probe.runtime_clone_root()
        if root is None:
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


def _check_slack_drain_alive() -> bool:
    """FAIL when the slack-drain sidecar is failing every pass or has gone silent.

    The ``teatree-slack-listener`` service drains inbound Slack every ~15s
    (``deploy/entrypoint.sh`` ``slack_drain_loop``) and rewrites a heartbeat with
    its consecutive-failure count. A drain failing pass after pass (``t3 slack
    check`` erroring — Django won't boot, DB unreachable) or a heartbeat gone
    stale (the loop died or hung) both mean captured DMs never reach the answer
    pipeline: teatree reacts 👀 but silently stops answering. Best-effort — an
    absent/unreadable heartbeat (no sidecar on this box) degrades to a pass.
    """
    try:
        beat = _Probe.slack_drain_heartbeat()
        now = _now()
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Slack-drain check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if beat is None:
        return True
    stale_after = max(_DRAIN_HEARTBEAT_STALE_MULTIPLIER * beat.interval_seconds, _DRAIN_HEARTBEAT_STALE_FLOOR_SECONDS)
    age = (now - beat.updated_at).total_seconds()
    if age > stale_after:
        typer.echo(
            f"FAIL  Slack-drain heartbeat is stale ({int(age)}s old, past {stale_after}s) — the "
            f"`teatree-slack-listener` drain loop has died or hung, so inbound Slack is no longer drained "
            f"or answered. Restart it: `docker compose -p {_COMPOSE_PROJECT} up -d teatree-slack-listener`."
        )
        return False
    if beat.consecutive_failures >= _MAX_DRAIN_CONSECUTIVE_FAILURES:
        typer.echo(
            f"FAIL  Slack drain has failed {beat.consecutive_failures} passes in a row — `t3 slack check` "
            f"keeps erroring in the `teatree-slack-listener` sidecar, so captured DMs never get 👀-acked or "
            f"answered. Inspect `docker compose -p {_COMPOSE_PROJECT} logs teatree-slack-listener`."
        )
        return False
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
        _check_stranded_headless_task,
        _check_stale_loop_timer,
        _check_interactive_task_under_headless,
        _check_failed_tasks_on_live_tickets,
        _check_runtime_clone_on_default_branch,
        _check_slack_drain_alive,
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

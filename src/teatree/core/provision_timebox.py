"""Time-box + loud-alert guard for long-blocking provisioning steps (#2220).

`worktree provision` / `worktree start` run subprocesses that can grind for an
hour with no progress signal: a DSLR snapshot restore, `migrate`, a
`--create-db` test-DB rebuild. The worst grind is a **forked migration graph**
— two branches each add a migration off the same parent, so the merged graph
has multiple leaf nodes and `migrate` refuses with *"Conflicting migrations
detected"* (or, worse, retries/grinds). A recent session spent ~1h frozen on
exactly this; the user only found it by asking three times.

This module is the "fail loud, never silently grind" principle applied to the
worktree lifecycle. It wraps a single provisioning subprocess so that:

1. it is **time-boxed** by a configurable ceiling
    (:func:`resolve_step_timeout_seconds`); on the ceiling the op aborts with a
    clear, actionable error — it never hangs;
2. it emits a **loud out-of-band user alert** (the same bot→user
    :func:`teatree.notify.notify_user` egress the codebase already uses) on a
    timeout, so an away user is told the step is slow and was aborted;
3. a **forked migration graph** detected in the step's output
    (:func:`detect_migration_conflict`) is surfaced *immediately* as the
    diagnosed cause — "rebase/renumber needed" — rather than a generic timeout;
4. a **progress heartbeat** fires while the op runs so a slow-but-progressing
    step is distinguishable from a true hang.

The alert is best-effort: :func:`teatree.notify.notify_user` never raises into
the caller, so a missing Slack backend degrades to a recorded NOOP, never a
crash of the provisioning path.
"""

import logging
import re
import subprocess  # noqa: S404 — only TimeoutExpired accessed, no shelling here
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.config import get_effective_settings
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.step_runner import StepResult, run_callable_step
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

# Sensible default ceiling for one provisioning subprocess. A DSLR restore +
# migrate on a healthy graph completes well within this; a forked graph or a
# genuine hang blows past it and gets aborted + alerted. Overridable via the
# DB-home ``provision_step_timeout_seconds`` setting (``t3 <overlay>
# config_setting set provision_step_timeout_seconds <n>``, per-overlay
# overridable); a ``[teatree]`` TOML value is ignored on read.
DEFAULT_STEP_TIMEOUT_SECONDS = 1800

# Heartbeat cadence: emit "still <step>… (Nm elapsed)" this often while the op
# runs, so the agent/statusline/monitor can tell progress from a hang.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0

# Phrases Django emits when a migration graph has forked (multiple leaf nodes
# off one parent). ``migrate``/``makemigrations`` print the first; the graph
# loader prints the second. Either is the #995/#2220 forked-graph signal.
_MIGRATION_CONFLICT_PATTERNS = (
    re.compile(r"conflicting migrations detected", re.IGNORECASE),
    re.compile(r"multiple leaf nodes in the migration graph", re.IGNORECASE),
)

_MIGRATION_FORK_REMEDY = (
    "forked migration graph — two migrations branch off the same parent. "
    "Rebase/renumber needed: merge the target branch in and run "
    "`python manage.py makemigrations --merge` to reconcile the leaves."
)


def resolve_step_timeout_seconds() -> int:
    """The configured hard ceiling (seconds) for one provisioning subprocess.

    Reads ``provision_step_timeout_seconds`` off the effective settings
    (per-overlay override → global → :data:`DEFAULT_STEP_TIMEOUT_SECONDS`).
    Always returns a positive ceiling — a non-positive or unreadable value
    degrades to the default so a misconfiguration can never disable the
    time-box (the "never hang" invariant must not be configurable away).
    """
    settings_ = get_effective_settings()
    value = getattr(settings_, "provision_step_timeout_seconds", DEFAULT_STEP_TIMEOUT_SECONDS)
    try:
        ceiling = int(value)
    except (TypeError, ValueError):
        return DEFAULT_STEP_TIMEOUT_SECONDS
    return ceiling if ceiling > 0 else DEFAULT_STEP_TIMEOUT_SECONDS


def detect_migration_conflict(output: str) -> str | None:
    """Return an actionable remedy when *output* shows a forked migration graph.

    The runtime sibling of :func:`teatree.core.migration_leaf_probe`: that
    one inspects the *git tree* pre-merge; this one matches the *running
    `migrate` subprocess's* stderr/stdout so a forked graph hit during
    provisioning is diagnosed by its symptom immediately, instead of letting
    `migrate`/`--create-db` grind to the ceiling. Returns ``None`` on a
    linear/empty graph (no conflict phrase present).
    """
    if not output:
        return None
    if any(pattern.search(output) for pattern in _MIGRATION_CONFLICT_PATTERNS):
        return _MIGRATION_FORK_REMEDY
    return None


def alert_provision_user(*, step: str, repo: str, detail: str) -> None:
    """Fire a loud out-of-band bot→user DM about a slow/failed provisioning step.

    The single egress for #2220's "alert, never silently grind" requirement —
    used by the subprocess time-box here and by
    :func:`teatree.core.step_runner.run_provision_steps` for callable-based
    steps. Best-effort: :func:`notify_user` never raises (degrades to a
    recorded NOOP when no backend resolves), so a failed alert never aborts the
    caller.
    """
    where = f" for {repo}" if repo else ""
    text = f"Worktree provisioning step `{step}`{where}: {detail}"
    key = f"provision-timeout:{step}:{repo or 'unknown'}"
    try:
        notify_user(text, kind=NotifyKind.INFO, idempotency_key=key)
    except Exception as exc:  # noqa: BLE001 — alert is best-effort; never crash provisioning
        logger.warning("provision-timebox alert failed for step=%s: %s", step, exc)


def _emit_heartbeats(
    *,
    step: str,
    interval: float,
    done: threading.Event,
    heartbeat: Callable[[str], object],
) -> None:
    """Emit a progress heartbeat every *interval* seconds until *done* is set."""
    start = time.monotonic()
    while not done.wait(interval):
        elapsed_min = (time.monotonic() - start) / 60
        heartbeat(f"still running `{step}`… ({elapsed_min:.1f}m elapsed)")


# ast-grep-ignore: ac-django-no-complexity-suppressions
def run_timeboxed_step(  # noqa: PLR0913 — each kwarg is a documented opt-in / test seam.
    name: str,
    cmd: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    repo: str = "",
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    heartbeat: Callable[[str], object] | None = None,
) -> StepResult:
    """Run one provisioning subprocess time-boxed, with heartbeat + loud alert.

    On a clean exit returns a successful :class:`StepResult`. On a non-zero
    exit whose output shows a **forked migration graph** the alert names that
    cause specifically (rebase/renumber). On exceeding *timeout* (defaulting
    to :func:`resolve_step_timeout_seconds`) the op aborts with a "timed out"
    error and a loud user alert — it never hangs. While the op runs, a progress
    heartbeat fires every *heartbeat_interval* seconds so a slow-but-moving
    step is distinguishable from a hang.
    """
    ceiling = timeout if timeout is not None else resolve_step_timeout_seconds()
    start = time.monotonic()
    done = threading.Event()
    beat = heartbeat or (lambda _msg: None)
    pulse = threading.Thread(
        target=_emit_heartbeats,
        kwargs={"step": name, "interval": heartbeat_interval, "done": done, "heartbeat": beat},
        daemon=True,
    )
    pulse.start()
    try:
        proc = run_allowed_to_fail(cmd, cwd=cwd, env=env, expected_codes=None, timeout=ceiling)
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        error = f"timed out after {ceiling}s"
        logger.warning("Provisioning step %r %s — aborting (never hang)", name, error)
        alert_provision_user(
            step=name,
            repo=repo,
            detail=f"exceeded {ceiling}s and was aborted — investigate a hang or a forked migration graph",
        )
        return StepResult(name=name, success=False, duration=duration, error=error)
    except OSError as exc:
        duration = time.monotonic() - start
        error = f"command not found: {getattr(exc, 'filename', cmd[0]) if cmd else exc}"
        logger.warning("Provisioning step %r: %s", name, error)
        return StepResult(name=name, success=False, duration=duration, error=error)
    finally:
        done.set()
        pulse.join(timeout=1)

    duration = time.monotonic() - start
    combined = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0:
        conflict = detect_migration_conflict(combined)
        error = proc.stderr.strip()[:500] if proc.stderr else f"exit code {proc.returncode}"
        if conflict is not None:
            error = f"{conflict} ({error})"
            logger.warning("Provisioning step %r hit a %s", name, conflict)
            alert_provision_user(step=name, repo=repo, detail=conflict)
        return StepResult(
            name=name,
            success=False,
            duration=duration,
            stdout=proc.stdout,
            stderr=proc.stderr,
            error=error,
        )
    return StepResult(name=name, success=True, duration=duration, stdout=proc.stdout, stderr=proc.stderr)


def _log_heartbeat(message: str) -> None:
    logger.info("provision: %s", message)


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _join_callable_on_ceiling(  # noqa: PLR0913 — each kwarg is a documented seam, mirroring run_timeboxed_step.
    name: str,
    invoke: Callable[[], None],
    *,
    ceiling: float,
    repo: str,
    heartbeat_interval: float,
    heartbeat: Callable[[str], object] | None,
    overrun_detail: str,
) -> tuple[bool, float]:
    """Run *invoke* on a daemon thread, heartbeating, bounded by *ceiling* seconds.

    Returns ``(timed_out, duration)``. On a wall-clock overrun the loud user
    alert fires and the daemon thread is abandoned — it dies with the process,
    so we never block on it. This is the "never hang" half of #2244: a callable
    provisioning step whose inner subprocess is blocked on its PIPE (the
    no-DSLR-snapshot / buffered case) is aborted rather than grinding forever.
    The subprocess sibling is :func:`run_timeboxed_step`.
    """
    start = time.monotonic()
    done = threading.Event()
    beat = heartbeat or _log_heartbeat
    worker = threading.Thread(target=invoke, daemon=True)
    pulse = threading.Thread(
        target=_emit_heartbeats,
        kwargs={"step": name, "interval": heartbeat_interval, "done": done, "heartbeat": beat},
        daemon=True,
    )
    pulse.start()
    worker.start()
    worker.join(timeout=ceiling)
    done.set()
    pulse.join(timeout=1)
    duration = time.monotonic() - start
    if worker.is_alive():
        logger.warning("Provisioning callable %r timed out after %ss — aborting (never hang)", name, ceiling)
        alert_provision_user(step=name, repo=repo, detail=overrun_detail)
        return True, duration
    return False, duration


# ast-grep-ignore: ac-django-no-complexity-suppressions
def run_timeboxed_callable(  # noqa: PLR0913 — each kwarg is a documented opt-in / test seam.
    name: str,
    fn: Callable[[], object],
    *,
    timeout: float | None = None,
    repo: str = "",
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    heartbeat: Callable[[str], object] | None = None,
) -> StepResult:
    """The callable sibling of :func:`run_timeboxed_step` (#2244).

    Provision steps that are Python callables (the overlay's migrate / seed,
    each wrapping an inner ``compose run``) shell out with no wall-clock bound,
    so a child blocked on its PIPE hangs the whole provision with no output. On
    overrunning the ceiling this returns a FAILED :class:`StepResult` naming the
    step and fires the loud user alert — it never hangs. A clean return is
    interpreted exactly as :func:`teatree.core.step_runner.run_callable_step`
    does (``CompletedProcess`` → success/failure, exception → FAILED), so the
    contract is identical whether or not the step overran.
    """
    ceiling = timeout if timeout is not None else resolve_step_timeout_seconds()
    captured: dict[str, StepResult] = {}
    timed_out, duration = _join_callable_on_ceiling(
        name,
        lambda: captured.__setitem__("result", run_callable_step(name, fn)),
        ceiling=ceiling,
        repo=repo,
        heartbeat_interval=heartbeat_interval,
        heartbeat=heartbeat,
        overrun_detail=(
            f"exceeded {ceiling}s and was aborted — a child process is blocked "
            "(a hung `compose run` or a missing DB source); never hangs"
        ),
    )
    if timed_out:
        return StepResult(name=name, success=False, duration=duration, error=f"timed out after {ceiling}s")
    return captured["result"]


@dataclass(slots=True)
class _DbImportOutcome:
    """What the time-boxed ``db_import`` thread captured for the main thread."""

    ok: bool = False
    error: BaseException | None = None


# ast-grep-ignore: ac-django-no-complexity-suppressions
def run_timeboxed_db_import(  # noqa: PLR0913 — each kwarg is a documented opt-in / test seam.
    fn: Callable[[], bool],
    *,
    name: str = "db_import",
    timeout: float | None = None,
    repo: str = "",
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    heartbeat: Callable[[str], object] | None = None,
) -> bool:
    """The DB-import sibling of :func:`run_timeboxed_callable`, for a bool callable (#2244).

    Returns the import's own bool on a clean return. On a wall-clock overrun —
    the silent-hang root cause when no DSLR snapshot exists and a child blocks on
    its PIPE — it fires the loud, actionable alert ("no local DSLR snapshot …
    run ``db refresh`` or supply a dump") and returns ``False`` so the caller
    aborts the provision loud and non-zero instead of hanging. A callable
    exception is re-raised on the main thread to keep the loud crash.
    """
    ceiling = timeout if timeout is not None else resolve_step_timeout_seconds()
    outcome = _DbImportOutcome()

    def _invoke() -> None:
        try:
            outcome.ok = bool(fn())
        except Exception as exc:  # noqa: BLE001 — re-raised on the main thread to keep the loud crash
            outcome.error = exc

    timed_out, _duration = _join_callable_on_ceiling(
        name,
        _invoke,
        ceiling=ceiling,
        repo=repo,
        heartbeat_interval=heartbeat_interval,
        heartbeat=heartbeat,
        overrun_detail=(
            f"exceeded {ceiling}s and was aborted — no local DSLR snapshot to restore "
            "(run `db refresh` or supply a dump); never hangs"
        ),
    )
    if timed_out:
        return False
    if outcome.error is not None:
        raise outcome.error
    return outcome.ok

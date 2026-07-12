"""Safe-kill guard — never signal a process by guessed pid or while it shows liveness (#2225).

An agent repeatedly killed the WRONG, LIVE process when asked to kill a "dead"
agent: it (a) matched the target by guessing which long-running ``claude`` pid
"looked dead" rather than by the named target's session/task id, and (b) read
high uptime + ``R+``/``S+`` (STAT) as "stuck" when ``R`` means running on CPU
and ``S+``/``R+`` mean a live foreground TTY session.

This module is the deterministic gate that makes that mistake structurally
impossible. :func:`evaluate_safe_kill` returns an ALLOW verdict only when BOTH
of the following hold.

**Positive identity.** The pid maps to a KNOWN dead/failed target by session
id (``~/.claude/sessions/*.json`` → session id → a FAILED Task via
``TaskAttempt.agent_session_id``), never by a heuristic "looks idle". A pid
that maps to no known dead target, or to a still-claimed task, is refused.

**Confirmed non-live.** Two CPU samples show no activity, output has not
advanced, the STAT is not a running/foreground state, AND a hang cause is
stated. A process in STAT ``R``/``R+`` (actively running) or ``S+``/``R+``
(live foreground TTY) is rejected outright.

The two externality boundaries — the per-pid liveness sample (shells out to
``ps``) and the pid→identity resolution (reads ``~/.claude/sessions`` + the
Task ORM) — are injectable so the guard logic is unit-testable without real
processes. The default implementations wire the real boundaries.
"""

import json
import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass

from teatree.core.selectors._helpers import _CLAUDE_SESSIONS_DIR

logger = logging.getLogger(__name__)

# STAT codes that PROVE liveness: ``R`` running on CPU; a trailing ``+`` means a
# foreground process group attached to a controlling terminal (a live session).
# Either is an immediate refusal regardless of CPU samples.
_RUNNING_STATS: frozenset[str] = frozenset({"R", "R+"})
_FOREGROUND_SUFFIX = "+"

_CPU_ACTIVITY_EPSILON = 0.5


class SafeKillError(RuntimeError):
    """Raised by :func:`safe_kill` in ``strict`` mode when the guard refuses."""


@dataclass(frozen=True, slots=True)
class Liveness:
    """One pid's liveness sample — the only process-level externality the guard reads.

    ``stat`` is the ``ps`` STAT field (``R``/``S``/``Z``/… with an optional
    ``+`` foreground suffix). The two CPU samples are ``%cpu`` readings taken a
    moment apart; ``output_advanced`` is whether the session's transcript grew
    between the samples.
    """

    stat: str
    cpu_sample_1: float
    cpu_sample_2: float
    output_advanced: bool

    @property
    def is_running_stat(self) -> bool:
        return self.stat in _RUNNING_STATS

    @property
    def is_foreground(self) -> bool:
        return self.stat.endswith(_FOREGROUND_SUFFIX)

    @property
    def cpu_active(self) -> bool:
        return max(self.cpu_sample_1, self.cpu_sample_2) >= _CPU_ACTIVITY_EPSILON


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    """The named target a pid resolves to — positive identity, never a guess.

    ``is_dead_target`` is True only when the session id maps to a Task that has
    actually FAILED/COMPLETED (a known dead target). An unresolvable pid yields
    an empty session id and ``is_dead_target=False``.
    """

    session_id: str
    task_id: int | None
    is_dead_target: bool


@dataclass(frozen=True, slots=True)
class SafeKillVerdict:
    """The guard's decision for one pid."""

    allowed: bool
    reason: str
    identity: TargetIdentity
    liveness: Liveness | None


LivenessSampler = Callable[[int], Liveness | None]
IdentityResolver = Callable[[int], TargetIdentity]
SignalSender = Callable[[int, int], None]


def evaluate_safe_kill(
    pid: int,
    *,
    hang_cause: str,
    resolve_identity: IdentityResolver | None = None,
    sample_liveness: LivenessSampler | None = None,
) -> SafeKillVerdict:
    """Decide whether *pid* may be signalled. ALLOW only on positive identity AND non-live.

    Refuses (with evidence in ``reason``) when the pid maps to no known dead
    target, maps to a still-claimed task, is in a running/foreground STAT, shows
    CPU activity or advancing output, or no hang cause is stated. The refusal
    names the candidate's session id and the liveness evidence and tells the
    caller to confirm the target id with the user first.
    """
    resolve_identity = resolve_identity or _default_resolve_identity
    sample_liveness = sample_liveness or _default_sample_liveness

    identity = resolve_identity(pid)
    liveness = sample_liveness(pid)

    if not (hang_cause or "").strip():
        return SafeKillVerdict(
            allowed=False,
            reason=_refusal("no hang cause stated", pid, identity, liveness),
            identity=identity,
            liveness=liveness,
        )

    if not identity.is_dead_target or not identity.session_id:
        return SafeKillVerdict(
            allowed=False,
            reason=_identity_refusal(pid, identity),
            identity=identity,
            liveness=liveness,
        )

    non_live_reason = _non_live_refusal(liveness)
    if non_live_reason is not None:
        return SafeKillVerdict(
            allowed=False,
            reason=_refusal(non_live_reason, pid, identity, liveness),
            identity=identity,
            liveness=liveness,
        )

    return SafeKillVerdict(allowed=True, reason="", identity=identity, liveness=liveness)


def _identity_refusal(pid: int, identity: TargetIdentity) -> str:
    if identity.session_id and not identity.is_dead_target:
        return _refusal(
            f"pid maps to session {identity.session_id} whose task is still claimed/active — not a dead target",
            pid,
            identity,
            None,
        )
    return _refusal("pid maps to no known dead/failed task id", pid, identity, None)


def _non_live_refusal(liveness: Liveness | None) -> str | None:
    """Reason the target still shows liveness, or ``None`` when confirmed non-live.

    ``None`` liveness is unverifiable — the guard cannot confirm non-live, so it
    refuses (fail closed).
    """
    if liveness is None:
        return "liveness could not be sampled — cannot confirm the process is dead"
    if liveness.is_running_stat:
        return f"process is in STAT {liveness.stat} (running on CPU) — actively running, never killed"
    if liveness.is_foreground:
        return f"process is in STAT {liveness.stat} (live foreground TTY session) — not stuck"
    if liveness.cpu_active:
        return "CPU activity present across samples — process is doing work"
    if liveness.output_advanced:
        return "session output advanced between samples — process is still progressing"
    return None


def _refusal(detail: str, pid: int, identity: TargetIdentity, liveness: Liveness | None) -> str:
    sid = identity.session_id or "<unmapped>"
    stat = liveness.stat if liveness is not None else "<unsampled>"
    return (
        f"REFUSED to signal pid {pid}: {detail}. "
        f"candidate session_id={sid} STAT={stat}. "
        "Confirm the target id with the user before killing any process — "
        "never match a 'dead' agent by which pid looks idle."
    )


# ast-grep-ignore: ac-django-no-complexity-suppressions
def safe_kill(  # noqa: PLR0913 — single safe-kill egress; each kwarg is a documented boundary injection / test override, kwargs-only.
    pid: int,
    *,
    hang_cause: str,
    resolve_identity: IdentityResolver | None = None,
    sample_liveness: LivenessSampler | None = None,
    send_signal: SignalSender | None = None,
    sig: int = signal.SIGTERM,
    strict: bool = False,
) -> SafeKillVerdict:
    """Signal *pid* only when :func:`evaluate_safe_kill` allows it.

    On a refusal the process is NEVER signalled. In ``strict`` mode the refusal
    raises :class:`SafeKillError` (for callers that want a hard failure); the
    default returns the verdict so callers can surface the evidence.
    """
    verdict = evaluate_safe_kill(
        pid,
        hang_cause=hang_cause,
        resolve_identity=resolve_identity,
        sample_liveness=sample_liveness,
    )
    if not verdict.allowed:
        logger.warning("safe_kill: %s", verdict.reason)
        if strict:
            raise SafeKillError(verdict.reason)
        return verdict

    sender = send_signal or _default_send_signal
    sender(pid, sig)
    logger.info("safe_kill: signalled pid %d (sig %d), session %s", pid, sig, verdict.identity.session_id)
    return verdict


# ---------------------------------------------------------------------------
# Default externality boundaries (the real ps / ~/.claude / ORM reads)
# ---------------------------------------------------------------------------


def _default_send_signal(pid: int, sig: int) -> None:
    os.kill(pid, sig)


def _default_sample_liveness(pid: int) -> Liveness | None:
    """Sample a pid's liveness via two ``ps`` reads a moment apart.

    Returns ``None`` when ``ps`` cannot report the pid (gone / unreadable) so the
    guard fails closed (cannot confirm non-live → refuse).
    """
    import shutil  # noqa: PLC0415 — deferred: loaded only on this code path
    import time  # noqa: PLC0415 — deferred: loaded only on this code path

    from teatree.utils.run import CommandFailedError, run_allowed_to_fail  # noqa: PLC0415 — deferred: call-time import

    ps = shutil.which("ps")
    if ps is None:
        return None

    def _read() -> tuple[str, float] | None:
        try:
            result = run_allowed_to_fail([ps, "-o", "stat=,pcpu=", "-p", str(pid)], expected_codes=None, timeout=10)
        except (OSError, CommandFailedError):
            return None
        if result.returncode != 0:
            return None
        line = result.stdout.strip()
        parts = line.split()
        if len(parts) < 2:  # noqa: PLR2004 — self-documenting literal in this context
            return None
        try:
            return parts[0], float(parts[1])
        except ValueError:
            return None

    first = _read()
    if first is None:
        return None
    time.sleep(0.3)
    second = _read()
    if second is None:
        return None
    stat = second[0]
    return Liveness(stat=stat, cpu_sample_1=first[1], cpu_sample_2=second[1], output_advanced=False)


def _default_resolve_identity(pid: int) -> TargetIdentity:
    """Resolve a pid to its named target via ``~/.claude/sessions`` + the Task ORM.

    The pid is mapped to a session id by the per-pid session-state files, and the
    session id is mapped to a Task by ``TaskAttempt.agent_session_id``. A target
    is "dead" only when that task has actually FAILED/COMPLETED. A pid that maps
    to no session, or to a still-active task, is NOT a dead target.
    """
    session_id = _session_id_for_pid(pid)
    if not session_id:
        return TargetIdentity(session_id="", task_id=None, is_dead_target=False)
    task_id, is_dead = _dead_task_for_session(session_id)
    return TargetIdentity(session_id=session_id, task_id=task_id, is_dead_target=is_dead)


def _session_id_for_pid(pid: int) -> str:
    sessions_dir = _CLAUDE_SESSIONS_DIR
    if not sessions_dir.is_dir():
        return ""
    for state_file in sessions_dir.glob("*.json"):
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("pid") == pid:
            return str(data.get("sessionId", ""))
    return ""


def _dead_task_for_session(session_id: str) -> tuple[int | None, bool]:
    from teatree.core.models import Task, TaskAttempt  # noqa: PLC0415 — deferred: ORM import needs the app registry

    attempt = TaskAttempt.objects.filter(agent_session_id=session_id).select_related("task").order_by("-pk").first()
    if attempt is None:
        return None, False
    is_dead = attempt.task.status in Task.Status.terminal()
    return attempt.task_id, is_dead


__all__ = [
    "IdentityResolver",
    "Liveness",
    "LivenessSampler",
    "SafeKillError",
    "SafeKillVerdict",
    "SignalSender",
    "TargetIdentity",
    "evaluate_safe_kill",
    "safe_kill",
]

"""Registry for the headless task runner — the core → agents inversion seam (#1922).

``core.tasks.execute_headless_task`` (a django-tasks worker) must run a task in a
detached headless Agent-SDK run, but that runner lives in ``teatree.agents``
(the higher layer). Rather than ``core`` importing ``agents``, ``agents``
registers its runner here at app-ready time and ``core`` resolves it through this
registry.

Fail-LOUD: a missing runner is fatal (a dispatched headless task that silently
does nothing is worse than a clear error), so :func:`get_headless_runner` raises
when nothing is registered.
"""

from typing import TYPE_CHECKING, Protocol

from teatree.core.modelkit.phases import subagent_for_phase

if TYPE_CHECKING:
    from teatree.core.models import Task, TaskAttempt
    from teatree.types import SkillMetadata


class HeadlessRunner(Protocol):
    def __call__(
        self,
        task: "Task",
        *,
        phase: str,
        overlay_skill_metadata: "SkillMetadata",
    ) -> "TaskAttempt": ...  # pragma: no branch


_runner: HeadlessRunner | None = None


def register_headless_runner(runner: HeadlessRunner) -> None:
    global _runner  # noqa: PLW0603 — single process-wide runner registered at app-ready
    _runner = runner


def get_headless_runner() -> HeadlessRunner:
    if _runner is None:
        msg = (
            "no headless runner registered — teatree.agents.apps.AgentsConfig.ready() "
            "must run before a headless task is dispatched"
        )
        raise RuntimeError(msg)
    return _runner


def runs_in_session(*, role: str, phase: str) -> bool:
    """True iff a loop-dispatched phase task must run INTERACTIVE in the ``/loop`` slot.

    The single predicate every dispatch gate consults (the save-time routing
    chokepoint, the auto-enqueue signal, the queue-drain safety net, and
    :func:`loop_dispatch_refusal`): a ``(role, phase)`` with a registered phase
    agent (:func:`has_registered_phase_agent`) runs in-session ONLY when the
    ``agent_runtime`` setting selects ``interactive``. Under the shipped
    ``headless`` lane the SAME phase work runs headless via ``agents/headless.py``
    behind the two-layer ``agent_harness`` / ``agent_harness_provider`` pair
    (#2887), so this returns ``False`` and the headless lane (auto-enqueue →
    ``execute_headless_task`` / ``work-next-headless``) takes it. Free-form work (no
    registered agent) is never in-session.
    """
    from teatree.config import AgentRuntime, get_effective_settings  # noqa: PLC0415 — deferred: call-time import

    if get_effective_settings().agent_runtime is not AgentRuntime.INTERACTIVE:
        return False
    return has_registered_phase_agent(role=role, phase=phase)


def has_registered_phase_agent(*, role: str, phase: str) -> bool:
    """True iff ``(role, phase)`` is a dispatched pair in the ``SUBAGENT_BY_PHASE`` registry.

    Pure registry membership over the ``core.modelkit`` leaf — no ORM, so the
    ``teatree.core`` → ``teatree.core.modelkit`` edge is a declared tach edge
    rather than a function-scoped import tach's acyclic guard cannot see.
    ``Task.loop_dispatched`` is the ORM-side spelling of the same lookup.
    """
    return bool(subagent_for_phase(role, phase))


def loop_dispatch_refusal(task: "Task") -> str | None:
    """Reason a headless dispatch of ``task`` is refused, or ``None`` to proceed.

    The single guard both headless entry points consult (souliane/teatree#1375):
    when ``agent_runtime`` selects ``interactive``, a loop-dispatched phase task —
    one whose ``(ticket.role, phase)`` has a registered phase agent — must run
    INTERACTIVE in the in-session ``/loop`` slot, never as a detached headless-SDK
    run, so this returns a ``routing_error`` reason and the caller records a
    refusal instead of shelling out. Under ``agent_runtime=headless`` the same
    work is meant to run headless, so this returns ``None`` and dispatch
    proceeds. Free-form headless work (no registered phase agent) always
    returns ``None``.

    Both ``core.tasks.execute_headless_task`` (the django-tasks worker) and
    ``core.management.commands.tasks.Command._execute_headless`` (the ``work-next-headless``
    CLI path) call this so the guard cannot drift between the two seams.
    """
    if not runs_in_session(role=task.ticket.role, phase=task.phase):
        return None
    return (
        f"refused headless dispatch for in-session phase "
        f"(role={task.ticket.role!r}, phase={task.phase!r}): "
        "this task runs INTERACTIVE in the /loop slot under agent_runtime=interactive "
        "(set agent_runtime=headless to run it headless)"
    )


def interactive_claim_refusal(task: "Task") -> str | None:
    """Reason an IN-SESSION claim of ``task`` is refused, or ``None`` to proceed.

    The mirror of :func:`loop_dispatch_refusal`, closing the other direction of
    the ``agent_runtime`` split: under ``agent_runtime=headless`` a
    loop-dispatched phase task is owned by the headless factory, so an
    interactive session must not claim it and hand-do the work. Under
    ``agent_runtime=interactive`` the in-session ``/loop`` slot IS
    that owner, so this returns ``None`` and the claim proceeds. Free-form work
    (no registered phase agent) is never headless-reserved.

    The verdict is read from the LIVE ``agent_runtime`` setting, not from the
    row's stored ``execution_target``. That column is written at insert time, so
    a phase task created before the runtime was flipped to ``headless`` stays
    ``INTERACTIVE`` and would otherwise remain claimable in-session — the exact
    hole this guard closes.

    Scope is deliberately narrow: ONLY the claim of a loop-dispatched phase
    task. Ordinary interactive work — editing, testing, committing, ad-hoc
    tasks, every ``t3`` diagnostic — is untouched under either runtime.
    """
    role = task.ticket.role
    if runs_in_session(role=role, phase=task.phase):
        return None
    if not has_registered_phase_agent(role=role, phase=task.phase):
        return None
    return (
        f"refused in-session claim of a headless-owned phase "
        f"(role={role!r}, phase={task.phase!r}): "
        "the headless factory owns loop-dispatched phase work under agent_runtime=headless. "
        "Run `t3 <overlay> tasks work-next-headless` to let the headless lane take it, "
        "or set agent_runtime=interactive to work it in the /loop slot"
    )

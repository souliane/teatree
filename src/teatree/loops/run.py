"""Per-loop autonomous runner — the real ``script`` entry point (#2513).

This is the on-disk entry point migration ``0080`` points every script-backed
:class:`teatree.core.models.loop.Loop` at (``script="src/teatree/loops/run.py"``)
— a file that did not exist until this module landed. There is no central/main
tick: each ``Loop`` row is autonomous and runs EITHER its on-disk ``script`` OR
its ``prompt`` (a :class:`teatree.core.models.prompt.Prompt`), the loop XOR.

:func:`run_loop` is the single dispatch seam. Given a loop name it loads the row
and routes by which side of the XOR is set:

- **script loop** → run that loop's registered scan unit through the existing
    scoped-tick machinery (``teatree.loops.scoped_tick.run_scoped_tick``). The
    domain scanners under :mod:`teatree.loops` stay the Python the loop invokes
    — the ``script`` only says *how* to invoke, it is not new behaviour.
- **prompt loop** → return the prompt's body as the instruction to dispatch
    (e.g. to a sub-agent). The dispatch itself is the caller's job; this runner
    resolves WHICH instruction the row carries.

The two dispatchers are injected so tests exercise the routing without running a
real tick or spawning a sub-agent. The runner NEVER ticks on import and is not
wired into the live 12-minute loop — invoking it is an explicit action.
"""

from collections.abc import Callable
from dataclasses import dataclass


class UnknownLoopError(LookupError):
    """No :class:`Loop` row is named the requested name."""


class LoopNotRunnableError(ValueError):
    """A loop row carries neither a ``prompt`` FK nor a ``script`` (XOR violated)."""


@dataclass(frozen=True, slots=True)
class LoopRunResult:
    """The structured outcome of running one autonomous loop.

    ``kind`` is ``"script"`` or ``"prompt"`` — which side of the XOR fired.
    ``detail`` is the resolved invocation: the scoped-tick outcome for a script
    loop, or the prompt body for a prompt loop. ``loop_name`` echoes the row.
    """

    loop_name: str
    kind: str
    detail: object


def _default_run_script(loop_name: str) -> object:
    """Run a script loop's scan unit through the existing scoped tick."""
    from teatree.core.backend_factory import iter_overlay_backends  # noqa: PLC0415
    from teatree.loops.orchestrator import TickRequest  # noqa: PLC0415
    from teatree.loops.scoped_tick import run_scoped_tick  # noqa: PLC0415

    return run_scoped_tick(loop_name, TickRequest(backends=iter_overlay_backends()))


def _default_run_prompt(body: str) -> object:
    """Resolve a prompt loop to its instruction body (caller dispatches it)."""
    return body


def run_loop(
    name: str,
    *,
    run_script: Callable[[str], object] = _default_run_script,
    run_prompt: Callable[[str], object] = _default_run_prompt,
) -> LoopRunResult:
    """Run the autonomous loop named *name* by the side of its XOR that is set.

    Raises :class:`UnknownLoopError` if no row matches and
    :class:`LoopNotRunnableError` if a row somehow carries neither side (the DB
    CheckConstraint should make that unreachable, but the runner fails loud
    rather than silently no-op).
    """
    from teatree.core.models import Loop  # noqa: PLC0415

    loop = Loop.objects.filter(name=name).select_related("prompt").first()
    if loop is None:
        msg = f"no Loop row named {name!r}"
        raise UnknownLoopError(msg)
    if loop.script:
        return LoopRunResult(loop_name=name, kind="script", detail=run_script(name))
    if loop.prompt_id is not None:
        return LoopRunResult(loop_name=name, kind="prompt", detail=run_prompt(loop.prompt.body))
    msg = f"loop {name!r} has neither a script nor a prompt"
    raise LoopNotRunnableError(msg)

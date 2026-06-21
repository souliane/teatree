"""Per-loop autonomous runner — each ``script`` is the loop's OWN module (#2513).

There is no central/main tick and no shared runner: every ``Loop`` row is
autonomous and runs EITHER its on-disk ``script`` OR its ``prompt`` (a
:class:`teatree.core.models.prompt.Prompt`), the loop XOR. A script-backed row's
``script`` is its OWN module ``src/teatree/loops/<name>/loop.py`` — the file that
exposes that loop's ``MINI_LOOP`` — so the DB ``script`` column is PER-LOOP and
LOAD-BEARING: it names exactly which loop the row drives, never a value shared
across rows.

:func:`run_loop` is the single dispatch seam. Given a loop name it loads the row
and routes by which side of the XOR is set:

- **script loop** → resolve the row's OWN module from ``Loop.script``
    (:func:`script_path_to_loop_name` maps the path UP to the loop name and
    verifies it against the live registry), then run that loop's registered scan
    unit through the existing scoped-tick machinery
    (``teatree.loops.scoped_tick.run_scoped_tick``). A ``script`` that does NOT
    resolve to a real registered loop module (a stale shared ``run.py``, a path
    naming an unregistered loop) raises :class:`UnresolvableScriptError` LOUDLY —
    never a silent no-op. The domain scanners under :mod:`teatree.loops` stay the
    Python the loop invokes — the ``script`` only says *which* loop to invoke.
- **prompt loop** → return the prompt's body as the instruction to dispatch
    (e.g. to a sub-agent). The dispatch itself is the caller's job; this runner
    resolves WHICH instruction the row carries.

The two dispatchers are injected so tests exercise the routing without running a
real tick or spawning a sub-agent. The runner NEVER ticks on import and is not
wired into the live 12-minute loop — invoking it is an explicit action.
"""

from collections.abc import Callable
from dataclasses import dataclass

from teatree.loops.scoped_tick import run_scoped_tick

_LOOPS_PACKAGE_PREFIX = "src/teatree/loops/"
_LOOP_MODULE_SUFFIX = "/loop.py"


class UnknownLoopError(LookupError):
    """No :class:`Loop` row is named the requested name."""


class LoopNotRunnableError(ValueError):
    """A loop row carries neither a ``prompt`` FK nor a ``script`` (XOR violated)."""


class UnresolvableScriptError(ValueError):
    """A ``Loop.script`` path does not resolve to a real registered loop module.

    Raised for a stale shared ``run.py``, a path naming an unregistered loop, or
    any value that is not the canonical ``src/teatree/loops/<name>/loop.py`` shape
    for a registered loop. The runner fails LOUD rather than silently no-op, so a
    misleading ``script`` column can never run nothing.
    """


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


def parse_script_loop_name(script: str) -> str:
    """Parse the loop name out of a ``src/teatree/loops/<name>/loop.py`` path.

    The pure path-shape half of the normalization seam: it does NOT touch the
    registry. A value that is not the canonical per-loop module shape (a stale
    shared ``run.py``, an arbitrary path, a nested path) raises
    :class:`UnresolvableScriptError` — never silently strips. Callers that also
    need to confirm the name is a *registered* loop use
    :func:`script_path_to_loop_name` (the global registry) or look the parsed
    name up in a registry they already hold (the master's per-tick registry).
    """
    name = script.removeprefix(_LOOPS_PACKAGE_PREFIX).removesuffix(_LOOP_MODULE_SUFFIX)
    is_module_shape = (
        script.startswith(_LOOPS_PACKAGE_PREFIX) and script.endswith(_LOOP_MODULE_SUFFIX) and name and "/" not in name
    )
    if not is_module_shape:
        msg = f"script {script!r} is not a per-loop module (expected src/teatree/loops/<name>/loop.py)"
        raise UnresolvableScriptError(msg)
    return name


def script_path_to_loop_name(script: str) -> str:
    """Map a ``Loop.script`` path UP to its canonical loop name, verified against the registry.

    The on-disk path ``src/teatree/loops/<name>/loop.py`` and the loop NAME are
    two representations of one identity; the loop name (the registry key) is the
    canonical form. This is the full normalization seam: it parses the path
    (:func:`parse_script_loop_name`) and verifies the name against the live
    mini-loop registry. A path that is not the per-loop module shape, or names a
    loop with no registered ``MINI_LOOP``, raises :class:`UnresolvableScriptError`
    — never silently strips or returns a bogus name.
    """
    from teatree.loops.registry import iter_loops  # noqa: PLC0415

    name = parse_script_loop_name(script)
    if name not in {loop.name for loop in iter_loops()}:
        msg = f"script {script!r} names loop {name!r}, which has no registered MINI_LOOP"
        raise UnresolvableScriptError(msg)
    return name


def _default_run_script(loop_name: str) -> object:
    """Run the loop's OWN module (resolved from ``Loop.script``) via the scoped tick.

    Reads the row's ``script`` column, resolves it to the loop's own name, and
    runs THAT loop's scan unit. A stale/shared/unregistered ``script`` raises
    :class:`UnresolvableScriptError` rather than running nothing.
    """
    from teatree.core.backend_factory import iter_overlay_backends  # noqa: PLC0415
    from teatree.core.models import Loop  # noqa: PLC0415
    from teatree.loops.orchestrator import TickRequest  # noqa: PLC0415

    row = Loop.objects.filter(name=loop_name).first()
    if row is None:
        msg = f"no Loop row named {loop_name!r}"
        raise UnknownLoopError(msg)
    resolved = script_path_to_loop_name(row.script)
    return run_scoped_tick(resolved, TickRequest(backends=iter_overlay_backends()))


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

"""Path↔name helper for a loop's OWN on-disk module (#2513, #2650).

Each script-backed ``Loop`` row's ``script`` is its OWN module
``src/teatree/loops/<name>/loop.py`` (the file exposing that loop's ``MINI_LOOP``)
— the ``script`` column is PER-LOOP and load-bearing, never a value shared across
rows. :func:`parse_script_loop_name` is the single normalization seam that maps
such a path UP to the loop name the master tick dispatches
(:func:`teatree.loops.master.build_loop_table_jobs`).

This module is a pure path↔name helper, NOT a dispatch seam: there is no central
runner and no shared tick. The DB ``Loop`` table is the single source of truth and
``build_loop_table_jobs`` is the one driver (#2513); each enabled row runs as its
own native Claude ``/loop`` firing ``t3 loops tick --loop <name>`` (#2650).
"""

_LOOPS_PACKAGE_PREFIX = "src/teatree/loops/"
_LOOP_MODULE_SUFFIX = "/loop.py"


class UnresolvableScriptError(ValueError):
    """A ``Loop.script`` path does not resolve to the canonical per-loop module shape.

    Raised for a stale shared runner path, an arbitrary path, or any value that is
    not the ``src/teatree/loops/<name>/loop.py`` shape. The caller fails LOUD rather
    than silently dispatching nothing, so a misleading ``script`` column can never
    run the wrong loop.
    """


def parse_script_loop_name(script: str) -> str:
    """Parse the loop name out of a ``src/teatree/loops/<name>/loop.py`` path.

    The pure path-shape normalization seam: it does NOT touch the registry. A value
    that is not the canonical per-loop module shape (an arbitrary path, a nested
    path) raises :class:`UnresolvableScriptError` — never silently strips. The
    master's per-tick registry confirms the parsed name resolves to a registered
    loop.
    """
    name = script.removeprefix(_LOOPS_PACKAGE_PREFIX).removesuffix(_LOOP_MODULE_SUFFIX)
    is_module_shape = (
        script.startswith(_LOOPS_PACKAGE_PREFIX) and script.endswith(_LOOP_MODULE_SUFFIX) and name and "/" not in name
    )
    if not is_module_shape:
        msg = f"script {script!r} is not a per-loop module (expected src/teatree/loops/<name>/loop.py)"
        raise UnresolvableScriptError(msg)
    return name

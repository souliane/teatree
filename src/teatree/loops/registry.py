"""Discover :class:`MiniLoop` definitions via :mod:`pkgutil` package walk.

Each mini-loop subpackage under :mod:`teatree.loops` exposes a
module-level ``MINI_LOOP: MiniLoop`` constant in its ``loop`` submodule.
:func:`iter_loops` walks the subpackages, imports each ``loop`` module,
and returns the constants sorted alphabetically by name.

Only true subpackages are considered (``sub.ispkg``), so the top-level helper
modules (``base``, ``registry``, ``config``, ``master``, ``run``, …) are skipped
already; the named-exclusion set is a belt-and-suspenders guard for the few that
share the package namespace.
"""

import importlib
import logging
import pkgutil

import teatree.loops as _loops_pkg
from teatree.loops.base import MiniLoop

logger = logging.getLogger(__name__)

_HELPER_MODULES: frozenset[str] = frozenset(
    {"base", "registry", "config"},
)


def iter_loops() -> tuple[MiniLoop, ...]:
    """Walk ``teatree.loops`` subpackages and collect each ``MINI_LOOP``."""
    found: list[MiniLoop] = []
    for sub in pkgutil.iter_modules(_loops_pkg.__path__):
        if sub.name in _HELPER_MODULES:
            continue
        if not sub.ispkg:
            continue
        try:
            mod = importlib.import_module(f"teatree.loops.{sub.name}.loop")
        except ImportError as exc:
            logger.warning("Skipping loop %r — import failed: %s", sub.name, exc)
            continue
        mini_loop = getattr(mod, "MINI_LOOP", None)
        if not isinstance(mini_loop, MiniLoop):
            logger.warning("Skipping loop %r — no module-level MINI_LOOP constant", sub.name)
            continue
        found.append(mini_loop)
    return tuple(sorted(found, key=lambda m: m.name))

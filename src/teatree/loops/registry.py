"""Discover :class:`MiniLoop` definitions via :mod:`pkgutil` package walk.

Each mini-loop subpackage under :mod:`teatree.loops` exposes a
module-level ``MINI_LOOP: MiniLoop`` constant in its ``loop`` submodule.
:func:`iter_loops` walks the subpackages, imports each ``loop`` module,
and returns the constants sorted alphabetically by name.

Only true subpackages are considered (``sub.ispkg``), so the top-level helper
modules (``base``, ``registry``, ``config``, ``run``, …) — plain ``.py`` files,
not packages — are skipped by that check alone. Helper *subpackages* that carry
no ``loop`` submodule (e.g. ``shared``, holding cross-loop utilities) are skipped
silently: a missing ``teatree.loops.<sub>.loop`` is the expected shape, not an
error, and only a ``loop`` module that exists but fails to import warrants a
``WARNING``.
"""

import importlib
import logging
import pkgutil

import teatree.loops as _loops_pkg
from teatree.loops.base import MiniLoop

logger = logging.getLogger(__name__)


def iter_loops() -> tuple[MiniLoop, ...]:
    """Walk ``teatree.loops`` subpackages and collect each ``MINI_LOOP``."""
    found: list[MiniLoop] = []
    for sub in pkgutil.iter_modules(_loops_pkg.__path__):
        if not sub.ispkg:
            continue
        try:
            mod = importlib.import_module(f"teatree.loops.{sub.name}.loop")
        except ImportError as exc:
            missing_own_loop = isinstance(exc, ModuleNotFoundError) and exc.name == f"teatree.loops.{sub.name}.loop"
            if missing_own_loop:
                # Helper subpackage with no ``loop`` submodule — expected, not an error.
                logger.debug("Skipping %r — no loop submodule (helper package)", sub.name)
            else:
                # ``loop`` exists but its own import chain is broken — a real error.
                logger.warning("Skipping loop %r — import failed: %s", sub.name, exc)
            continue
        mini_loop = getattr(mod, "MINI_LOOP", None)
        if not isinstance(mini_loop, MiniLoop):
            logger.warning("Skipping loop %r — no module-level MINI_LOOP constant", sub.name)
            continue
        found.append(mini_loop)
    return tuple(sorted(found, key=lambda m: m.name))

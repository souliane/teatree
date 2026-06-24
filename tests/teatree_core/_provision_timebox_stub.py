"""Shared test seams modelling ``provision_timebox`` import failures (souliane/teatree#2664).

Two distinct, oft-confused failure shapes.

``provision_timebox_unimportable`` models the module ITSELF being absent — a
worktree torn down from a STALE base created before ``provision_timebox`` was
added in #2220. Teardown runs the worktree's OWN checkout (``uv --directory
<worktree> run``), so its interpreter cannot import the module the orchestrating
``t3`` added later. The lazy import raises a ``ModuleNotFoundError`` whose
``.name`` IS ``teatree.core.provision_timebox``, and ``run_step`` must degrade to
a plain subprocess run.

``provision_timebox_internally_broken`` models the module being PRESENT but its
own body hitting a broken transitive import (a missing dependency). Python
surfaces a ``ModuleNotFoundError`` whose ``.name`` is the missing DEPENDENCY, not
``provision_timebox``. ``run_step`` must NOT swallow this — silently degrading
would disable the timeout/heartbeat/alert for every healthy install and mask the
real bug — so it must PROPAGATE.
"""

import builtins
from typing import Self
from unittest.mock import patch

_PROVISION_TIMEBOX = "teatree.core.provision_timebox"

#: ``.name`` of the error a present-but-broken ``provision_timebox`` would raise
#: from its own body (a missing transitive dependency, NOT this module).
BROKEN_DEPENDENCY_NAME = "some_missing_transitive_dep"


class provision_timebox_unimportable:  # noqa: N801 — context-manager reads as a verb at the call site
    """Context manager: ``import teatree.core.provision_timebox`` raises ModuleNotFoundError (module absent)."""

    _real_import = builtins.__import__

    def __enter__(self) -> Self:
        self._patch = patch.object(builtins, "__import__", self._raising_import)
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._patch.stop()

    def _raising_import(self, name: str, *args: object, **kwargs: object) -> object:
        if name == _PROVISION_TIMEBOX:
            msg = f"No module named '{_PROVISION_TIMEBOX}'"
            raise ModuleNotFoundError(msg, name=name)
        return self._real_import(name, *args, **kwargs)


class provision_timebox_internally_broken:  # noqa: N801 — context-manager reads as a verb at the call site
    """Context manager: importing ``provision_timebox`` fails on its OWN broken transitive import.

    The module is present, so the error's ``.name`` is the missing DEPENDENCY —
    exactly what Python raises when a present module's body executes a
    ``from <missing_dep> import ...`` line.
    """

    _real_import = builtins.__import__

    def __enter__(self) -> Self:
        self._patch = patch.object(builtins, "__import__", self._raising_import)
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._patch.stop()

    def _raising_import(self, name: str, *args: object, **kwargs: object) -> object:
        if name == _PROVISION_TIMEBOX:
            msg = f"No module named '{BROKEN_DEPENDENCY_NAME}'"
            raise ModuleNotFoundError(msg, name=BROKEN_DEPENDENCY_NAME)
        return self._real_import(name, *args, **kwargs)

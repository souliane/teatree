"""Shared test seam: make ``provision_timebox`` unimportable (souliane/teatree#2664).

Models a worktree torn down from a STALE base — one created before
``provision_timebox`` was added (souliane/teatree#2220). Teardown runs the
worktree's OWN checkout (``uv --directory <worktree> run``), so its interpreter
cannot import the module the orchestrating ``t3`` added later; the lazy import in
``run_step`` then raised ``ModuleNotFoundError`` and aborted the whole teardown.
"""

import builtins
from typing import Self
from unittest.mock import patch

_PROVISION_TIMEBOX = "teatree.core.provision_timebox"


class provision_timebox_unimportable:  # noqa: N801 — context-manager reads as a verb at the call site
    """Context manager: ``import teatree.core.provision_timebox`` raises ModuleNotFoundError."""

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

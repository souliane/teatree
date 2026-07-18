"""Portable repo-quality hooks, shipped so any repo can run them via ``t3 hook run``.

Teatree's gate scripts live in ``scripts/hooks/`` and were wired only into
teatree's own ``.pre-commit-config.yaml`` — a downstream repo could adopt them
only by shimming through ``teatree.__file__`` (editable installs only) or by
body-copying the shell hooks (silent drift). This package is the packaged,
non-editable-safe home of the *deliberately portable* subset: the gates that
operate on ``git diff --cached`` in the current working directory, so they run
unchanged in any checkout. The registry and resolver live in
:mod:`teatree.hooks.portable._resolver`; the individual hooks are sibling
modules (Python) plus ``refuse-main-clone-commit.sh`` (shell).
"""

from teatree.hooks.portable._resolver import (
    PORTABLE_HOOKS,
    PortableHook,
    UnknownHookError,
    available_hook_names,
    run_hook,
)

__all__ = [
    "PORTABLE_HOOKS",
    "PortableHook",
    "UnknownHookError",
    "available_hook_names",
    "run_hook",
]

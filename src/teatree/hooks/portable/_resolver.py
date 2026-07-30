"""Registry and resolver for the portable repo-quality hooks (#3312).

Each hook name maps to a :class:`PortableHook`. Python hooks expose ``main() ->
int`` and run in-process; the shell hook runs as a subprocess. Exit codes pass
through unchanged. Names outside this registry — including internal-only
generators/sync checks (blueprint sync, CLI-reference sync, ...) — are refused
loudly, listing the available portable names; those are teatree-repo build
steps, not consumable gates. Re-exported from the package ``__init__``.
"""

import importlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import as_file, files
from typing import Literal

from teatree.utils.run import run_streamed

__all__ = [
    "PORTABLE_HOOKS",
    "PortableHook",
    "UnknownHookError",
    "available_hook_names",
    "run_hook",
]


@dataclass(frozen=True)
class PortableHook:
    """One consumable gate: a stable ``name`` and how to run it.

    ``kind`` is ``"python"`` (``target`` is a dotted module exposing ``main() ->
    int``, run in-process) or ``"shell"`` (``target`` is a script filename in
    this package, run as a subprocess).
    """

    name: str
    summary: str
    kind: Literal["python", "shell"]
    target: str


_PACKAGE = "teatree.hooks.portable"

PORTABLE_HOOKS: dict[str, PortableHook] = {
    hook.name: hook
    for hook in (
        PortableHook(
            "check_module_health",
            "Module-level architectural health ratchet (LOC / public-function caps).",
            "python",
            f"{_PACKAGE}.check_module_health",
        ),
        PortableHook(
            "check_no_silent_skip",
            "Ban unconditionally-disabled tests (skip / skipif(True)).",
            "python",
            f"{_PACKAGE}.check_no_silent_skip",
        ),
        PortableHook(
            "check_broad_except",
            "A broad-except handler must be observable, never fail open.",
            "python",
            f"{_PACKAGE}.check_broad_except",
        ),
        PortableHook(
            "check_test_shape",
            "Report-first test-shape check (integration/E2E ratio, duplicate shape).",
            "python",
            f"{_PACKAGE}.check_test_shape",
        ),
        PortableHook(
            "check_test_path_mirror",
            "Test files mirror their src/<pkg>/... module path (forward-guard).",
            "python",
            f"{_PACKAGE}.check_test_path_mirror",
        ),
        PortableHook(
            "check_pr_body_stray",
            "Refuse a hand-named PR/MR body scratch file staged inside the worktree.",
            "python",
            f"{_PACKAGE}.check_pr_body_stray",
        ),
        PortableHook(
            "refuse-main-clone-commit",
            "Worktree-first: refuse commits made in the main clone (#638, #2614).",
            "shell",
            "refuse-main-clone-commit.sh",
        ),
    )
}


class UnknownHookError(KeyError):
    """Raised when a name is not a packaged portable hook."""


def available_hook_names() -> list[str]:
    """The registered portable hook names, in registration order."""
    return list(PORTABLE_HOOKS)


def _run_python(target: str, args: Sequence[str]) -> int:
    """Run a Python hook in-process, passing ``args`` through its ``sys.argv``.

    The hooks read ``sys.argv`` (``check_module_health`` accepts ``--from-ref``);
    the others ignore extra args. ``sys.argv`` is saved and restored so an
    in-process invocation leaves the caller's argv untouched.
    """
    module = importlib.import_module(target)
    saved = sys.argv
    sys.argv = [target.rsplit(".", 1)[-1], *args]
    try:
        result = module.main()
    finally:
        sys.argv = saved
    return int(result or 0)


def _run_shell(filename: str, args: Sequence[str]) -> int:
    """Run a packaged shell hook as a subprocess, returning its exit code.

    ``importlib.resources`` resolves the script whether teatree is an editable
    checkout or a wheel install; ``as_file`` materialises it on disk so it can be
    executed. ``run_streamed`` keeps the hook's stdout inherited (its refuse
    messages reach the terminal) while passing the exit code through.
    """
    resource = files(_PACKAGE).joinpath(filename)
    with as_file(resource) as path:
        return run_streamed([str(path), *args], check=False)


def run_hook(name: str, args: Sequence[str] = ()) -> int:
    """Resolve ``name`` to a packaged portable hook and run it; return its exit code.

    Raises :class:`UnknownHookError` for a name outside :data:`PORTABLE_HOOKS`.
    """
    hook = PORTABLE_HOOKS.get(name)
    if hook is None:
        raise UnknownHookError(name)
    if hook.kind == "python":
        return _run_python(hook.target, args)
    return _run_shell(hook.target, args)

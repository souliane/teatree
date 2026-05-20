"""Self-heal a teatree install whose env drifted from ``pyproject.toml``.

When teatree adds a top-level dependency, an existing editable install
hits ``ModuleNotFoundError`` until the env's deps are re-synced.  This
module detects that drift and repairs **the environment that actually
executes ``t3``** — never a foreign ``uv tool`` env (the #805 class of
bug, where ``t3 setup`` printed ``OK Reinstalled`` then immediately
WARNed the deps were still missing because the repair touched a
different interpreter than the running one).

Extracted from ``cli/setup.py`` as a distinct concern (module-health).
"""

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from teatree.utils.dep_drift import (
    editable_source_path,
    find_missing_dependencies,
    running_env_is_uv_tool,
    running_prefix,
    running_python,
)
from teatree.utils.run import CompletedProcess, run_allowed_to_fail

DRIFT_GUARD_ENV = "_T3_DRIFT_REPAIR_ATTEMPTED"


def _run_captured(args: list[str], cwd: Path | None = None) -> CompletedProcess[str]:
    """Run a subprocess, capturing output and never raising on non-zero exit."""
    return run_allowed_to_fail(args, cwd=cwd, expected_codes=None)


@dataclass(slots=True)
class RepairPlan:
    """A concrete command that repairs the *running* interpreter's env.

    ``cmd`` is run as a subprocess; ``label`` is the human-readable command
    echoed to the user (and the exact manual fallback if the subprocess
    fails).  Both always describe the environment that actually executes
    ``t3`` — never a foreign ``uv tool`` env.
    """

    cmd: list[str]
    label: str


def resolve_repair_plan(missing: list[str]) -> RepairPlan | str:
    """Build a repair plan for the running interpreter, or a warning string.

    The repair target is the environment whose ``importlib.metadata`` was
    read to *detect* the drift (``sys.prefix`` of the running ``t3``).  When
    that env is ``uv tool``-managed, ``uv tool install --reinstall`` is the
    right resync.  When it is a plain editable install in a pyenv/virtualenv
    (a *different* env than any ``uv tool`` one), repairing must install into
    that running interpreter — ``uv tool install`` there would silently fix a
    foreign env and leave the running ``t3`` still broken.
    """
    if os.environ.get(DRIFT_GUARD_ENV):
        return (
            f"WARN  Dep drift repair already attempted but deps still missing: "
            f"{', '.join(missing)}\n"
            f"      Running interpreter: {running_python()}\n"
            f"      Manual fix: `{running_python()} -m pip install -e "
            f"{editable_source_path() or '<teatree-source>'}`."
        )
    source = editable_source_path()
    if source is None:
        return (
            f"WARN  Teatree is installed non-editable and is missing declared "
            f"deps: {', '.join(missing)}\n"
            f"      Reinstall into the running env: "
            f"`{running_python()} -m pip install --upgrade teatree`."
        )

    if running_env_is_uv_tool():
        uv_bin = shutil.which("uv")
        if not uv_bin:
            return (
                f"WARN  Editable install missing deps ({', '.join(missing)}) but "
                "`uv` is not on PATH — install uv and re-run `t3 setup`."
            )
        return RepairPlan(
            cmd=[uv_bin, "tool", "install", "--editable", str(source), "--reinstall"],
            label=f"uv tool install --editable {source} --reinstall",
        )

    # The running t3 is a plain editable install (pip install -e .) into a
    # pyenv/virtualenv that uv tool does not manage.  Repair THAT env.
    python = str(running_python())
    return RepairPlan(
        cmd=[python, "-m", "pip", "install", "-e", str(source)],
        label=f"{python} -m pip install -e {source}",
    )


def repair_dep_drift(repo: Path) -> bool:
    """Reinstall the running teatree if its env is missing declared deps.

    Detection (:func:`find_missing_dependencies`) and repair both target the
    environment that actually executes ``t3`` (``sys.prefix`` of the running
    process).  Repairing a different env — e.g. ``uv tool install`` when the
    running ``t3`` is a pyenv ``pip install -e`` — would print ``OK
    Reinstalled`` while leaving the running interpreter still broken.

    Returns ``True`` and ``execv``-replaces the process when a repair was
    triggered (so this never actually returns ``True`` from the caller's
    perspective).  Returns ``False`` when no drift is detected, when the
    install is non-editable (PyPI/wheel), or when the repair tool is
    unavailable.
    """
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return False
    missing = find_missing_dependencies(pyproject)
    if not missing:
        return False

    plan = resolve_repair_plan(missing)
    if isinstance(plan, str):
        typer.echo(plan)
        return False

    typer.echo(
        f"NOTE  Missing deps in running env ({running_prefix()}): {', '.join(missing)}.  Re-running `{plan.label}` …",
    )
    result = _run_captured(plan.cmd)
    if result.returncode != 0:
        typer.echo(f"WARN  Reinstall failed: {result.stderr.strip()}")
        typer.echo(f"      Manual fix: `{plan.label}`.")
        return False

    typer.echo("OK    Reinstalled — restarting `t3 setup` against the running env.")
    os.environ[DRIFT_GUARD_ENV] = "1"
    # Re-exec the *running* t3, not whatever `t3` happens to be first on PATH
    # (that may be a different shim/env than the one we just repaired).
    t3_bin = sys.argv[0] or shutil.which("t3") or "t3"
    os.execv(t3_bin, [t3_bin, *sys.argv[1:]])  # noqa: S606 — argv from sys.argv, no shell
    return True  # unreachable — execv replaces the process; here for the type-checker

"""``t3 doctor`` checks that the containerized ``t3`` workflow is intact (#3232).

Both gates report DRIFT — a workflow that was set up and has since decayed — never
mere absence. A native clone that never opted in has no launcher to shadow and no
volume to mount, so firing there would make ``t3 doctor check`` unable to exit 0 on
any checkout of core, which is an over-block rather than a finding.

:func:`_check_t3_launcher_managed` therefore FAILs only once a managed launcher is
installed: something else took the name on ``PATH``, or the launcher names a checkout
that is gone. :func:`_check_control_db_reachable` FAILs only for a directory something
is actually using — the container runtime's mounted volume, or one an operator named
through :data:`~teatree.paths.CONTROL_DB_DIR_ENV` — because that is where an unreadable
directory silently degrades every config read to a shipped default.

Both run on either side of the container boundary, because the only surviving
venue for ``t3`` is the container while the launcher they verify lives on the
host — reached there through :data:`~teatree.docker.workflow.CONTAINER_HOST_BIN_DIR`.
"""

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

import typer

from teatree.docker.workflow import (
    CONTAINER_HOST_BIN_DIR,
    DEPLOY_CHECKOUT_ENV,
    is_running_in_container,
    launcher_path,
    launcher_wrapper_target,
    read_managed_launcher,
    wrapper_path,
)

_REPAIR = f"Re-run `t3 setup` from the current checkout (or `${DEPLOY_CHECKOUT_ENV}/deploy/t3 setup`)."


def _stale_target_problem(script: str, expected: Path | None) -> str | None:
    """The problem with the checkout a managed launcher names, or ``None`` when sound.

    *expected* is the checkout this venue can prove the launcher should name, or
    ``None`` when it cannot — in which case the launcher's own target must merely
    still be an executable ``deploy/t3``, which only the host can observe.
    """
    target = launcher_wrapper_target(script)
    if target is None:
        return "carries no `exec` line naming a deploy/t3"
    if expected is not None:
        return None if target == expected else f"points at {target}, but this checkout's entry is {expected}"
    if not target.is_file() or not os.access(target, os.X_OK):
        return f"points at {target}, which is not an executable deploy/t3 — the checkout moved or was deleted"
    return None


def _host_launcher_problem(env: Mapping[str, str], which: Callable[[str], str | None]) -> str | None:
    """The problem with the ``t3`` this HOST resolves, or ``None`` when there is none.

    Absence is not drift. A checkout that never ran ``t3 setup`` has no launcher to
    shadow — reporting one there would fail every native clone of core — so a finding
    needs a managed launcher to exist first: either the one ``PATH`` resolves names a
    checkout that is gone, or one is installed at :func:`launcher_path` and ``PATH``
    resolves something else over it.
    """
    found = which("t3")
    script = read_managed_launcher(Path(found)) if found is not None else None
    if script is not None:
        problem = _stale_target_problem(script, None)
        return None if problem is None else f"The managed t3 launcher at {found} {problem}"
    if read_managed_launcher(launcher_path(env)) is None:
        return None
    return f"`t3` on PATH is {found or '<nothing>'}, not the managed container launcher at {launcher_path(env)}"


def _mounted_launcher_problem(env: Mapping[str, str], mount_dir: Path) -> str | None:
    """The problem with the HOST launcher reached through the mount, or ``None``.

    ``None`` also when there is no mount: a container ``deploy/t3`` did not start
    carries no window onto the host and must not invent a verdict about it.
    """
    if not mount_dir.is_dir():
        return None
    path = mount_dir / "t3"
    script = read_managed_launcher(path)
    if script is None:
        return "The host `t3` is not the managed container launcher"
    checkout = env.get(DEPLOY_CHECKOUT_ENV, "").strip()
    expected = wrapper_path(Path(checkout)) if checkout else None
    problem = _stale_target_problem(script, expected)
    return None if problem is None else f"The host `t3` launcher {problem}"


def _check_t3_launcher_managed(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    mount_dir: Path = CONTAINER_HOST_BIN_DIR,
) -> bool:
    """FAIL when the host ``t3`` is not a managed launcher for the current checkout.

    ``uv tool install``/``upgrade`` restores its own console script at the same
    path, and a relocated checkout leaves the launcher naming a directory that no
    longer exists; both revert ``t3`` to something that cannot read the control
    DB, and nothing else reports either.
    """
    resolved_env = env if env is not None else dict(os.environ)
    if is_running_in_container(resolved_env):
        problem = _mounted_launcher_problem(resolved_env, mount_dir)
    else:
        which_fn = which if which is not None else shutil.which
        problem = _host_launcher_problem(resolved_env, which_fn)
    if problem is None:
        return True
    typer.echo(
        f"FAIL  {problem}. Teatree runs only in Docker: a host t3 cannot reach the control "
        f"DB and silently resolves every setting from shipped defaults. {_REPAIR}"
    )
    return False


def _check_control_db_reachable(*, env: Mapping[str, str] | None = None) -> bool:
    """FAIL when a control-DB directory something is actually using cannot be read.

    Config resolves from the stored settings only while that directory is readable;
    when it is not, every ``ConfigSetting`` read silently returns a shipped default.
    The invariant is readability, not which directory it is — a dev checkout pointed at
    its own real database is healthy, and one that named no database at all has nothing
    to repair, so its absence is advisory rather than a finding.
    """
    from teatree.paths import CONTROL_DB_DIR_ENV, control_db_dir  # noqa: PLC0415 — deferred: keeps CLI startup light

    resolved_env = env if env is not None else dict(os.environ)
    directory = control_db_dir(resolved_env)
    if directory.is_dir() and os.access(directory, os.R_OK):
        return True
    in_use = is_running_in_container(resolved_env) or bool(resolved_env.get(CONTROL_DB_DIR_ENV, "").strip())
    if not directory.is_dir() and not in_use:
        typer.echo(
            f"WARN  No control DB directory at {directory}: this checkout has not been set up for "
            f"the containerized workflow, so every config read resolves from shipped defaults. Run "
            f"`t3 setup`, or point {CONTROL_DB_DIR_ENV} at a readable control DB directory."
        )
        return True
    reason = "does not exist" if not directory.is_dir() else "is not readable"
    typer.echo(
        f"FAIL  The control DB directory {directory} {reason}, so every config read "
        f"silently resolves from shipped defaults instead of the stored settings. It is a "
        f"Docker named volume that exists only inside the container — run t3 through the "
        f"managed launcher (`t3 setup` installs it), or point {CONTROL_DB_DIR_ENV} at a "
        f"readable control DB directory."
    )
    return False

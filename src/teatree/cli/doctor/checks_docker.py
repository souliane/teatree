"""``t3 doctor`` check that the containerized ``t3`` workflow is wired (#3232).

The headless deployment runs the CLI and every server exclusively in Docker; on
a host, ``t3 setup`` installs a shell alias pointing ``t3`` at the
container-wrapping entry (``deploy/t3``). This check verifies that once the
operator has opted in — an installed alias block — the pieces the wrapper
depends on are actually present and current (compose stack, executable wrapper,
``docker`` on PATH, non-stale alias path).

Scope-limited to avoid noise: it stays silent inside a container (the container
IS the runtime) and on a host that has NOT opted into the workflow (no alias
block — a plain native ``uv tool install --editable`` dev checkout). It is
surfacing-only (always returns ``True``), like the other advisory doctor probes.
"""

import contextlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import typer

from teatree.docker.workflow import installed_alias_block, is_running_in_container, workflow_problems


def _default_rc_files() -> list[Path]:
    """The shell rc files ``t3 setup`` may have installed the alias into."""
    home = Path.home()
    return [home / ".bashrc", home / ".zshrc"]


def _safe_main_clone() -> Path | None:
    """Resolve the teatree main clone, swallowing any resolution failure.

    A doctor check must never crash the doctor run, and clone resolution reaches
    into the filesystem / env — so any failure degrades to "cannot verify" (None).
    """
    with contextlib.suppress(Exception):
        from teatree.cli.setup.clone import find_main_clone  # noqa: PLC0415 (deferred import)

        return find_main_clone()
    return None


def _check_docker_workflow_wired(
    *,
    env: dict[str, str] | None = None,
    repo: Path | None = None,
    rc_paths: list[Path] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> bool:
    """WARN when an opted-in containerized ``t3`` workflow is wired but broken.

    Silent (returns ``True``) inside a container, when the main clone cannot be
    resolved, and when no alias block is installed (the operator has not opted
    into the Docker workflow). Once opted in, any missing/stale piece the wrapper
    needs is surfaced as a single actionable WARN pointing back at ``t3 setup``.
    Surfacing-only — never gates the doctor exit code.
    """
    resolved_env = env if env is not None else dict(os.environ)
    if is_running_in_container(resolved_env):
        return True

    resolved_repo = repo if repo is not None else _safe_main_clone()
    if resolved_repo is None:
        return True

    paths = rc_paths if rc_paths is not None else _default_rc_files()
    installed = installed_alias_block(paths)
    if installed is None:
        return True  # not opted into the containerized workflow — nothing to verify

    which_fn = which if which is not None else shutil.which
    problems = workflow_problems(resolved_repo, installed, which_fn)
    if problems:
        typer.echo(
            "WARN  Containerized t3 workflow is wired but incomplete: "
            + "; ".join(problems)
            + " — re-run `t3 setup` (or fix the deploy checkout)."
        )
    return True

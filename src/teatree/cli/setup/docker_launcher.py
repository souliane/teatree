"""Install the containerized-``t3`` launcher during ``t3 setup`` (#3232).

``t3`` is one fixed thing: an executable launcher on ``PATH`` that ``exec``s the
main checkout's ``deploy/t3``, so every caller — an interactive shell, a script,
a git hook, cron, a sub-agent — runs the same teatree in the same container.
There is no second ``t3``, and no shell alias.

That leaves nobody on the host to WRITE the launcher once the uv-installed tool
is retired, which would make a relocated checkout unrepairable. So the container
writes it instead, through the narrow bind mount of the host's ``PATH`` bin dir
at :data:`~teatree.docker.workflow.CONTAINER_HOST_BIN_DIR`, rendering the launcher
against the HOST checkout named by ``$TEATREE_DEPLOY_CHECKOUT``. A container
without that mount was not started by ``deploy/t3`` and must not guess: it says
so and leaves the host alone.

On the host the uv-installed tool is removed once the launcher is verified as the
``t3`` on ``PATH``. The order is load-bearing: a failed launcher write must never
leave the operator with no ``t3`` at all. Best-effort throughout — a refusal or
an unwritable path WARNs with the manual fix and never aborts setup.
"""

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from teatree.cli.setup._process import run_captured
from teatree.docker.workflow import (
    CONTAINER_HOST_BIN_DIR,
    DEPLOY_CHECKOUT_ENV,
    LauncherInstall,
    install_launcher,
    is_running_in_container,
    launcher_path,
    wrapper_path,
)

Echo = Callable[[str], None]


def _wrapper_is_executable(repo: Path) -> bool:
    """Whether *repo*'s ``deploy/t3`` is there to be ``exec``ed — the launcher's whole job.

    Only the venue that will RUN the launcher can answer it, so it lives beside the HOST
    install rather than in the shared workflow: a path resolved on one side of the
    container boundary and written to the other names nothing on arrival, and bash
    reports that as ``exec: … cannot execute`` on every single invocation of ``t3``.
    """
    target = wrapper_path(repo)
    return target.is_file() and os.access(target, os.X_OK)


_BLOCKED = {
    LauncherInstall.REFUSED,
    LauncherInstall.UNWRITABLE,
    LauncherInstall.UNVERIFIED,
    LauncherInstall.NO_TARGET,
}


class DockerLauncherInstaller:
    """Compose unit: make ``t3`` on ``PATH`` the container-wrapping launcher."""

    def __init__(
        self,
        repo: Path,
        env: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] | None = None,
        host_bin_mount: Path = CONTAINER_HOST_BIN_DIR,
    ) -> None:
        self._repo = repo
        self._env = dict(env) if env is not None else dict(os.environ)
        self._which = which if which is not None else shutil.which
        self._host_bin_mount = host_bin_mount

    def launcher_path(self) -> Path:
        """Where this installer writes the launcher."""
        return launcher_path(self._env)

    def install(self, *, echo: Echo) -> None:
        """Write the launcher, on whichever side of the container boundary this runs."""
        if is_running_in_container(self._env):
            self._install_through_mount(echo=echo)
            return
        self._install_on_host(echo=echo)

    def _install_on_host(self, *, echo: Echo) -> None:
        """Write the launcher directly, then retire the uv-installed host tool behind it.

        The exec target is checked FIRST, which only a host can do: publishing a launcher
        naming a ``deploy/t3`` that is not there replaces a working ``t3`` with one that
        answers ``exec: … cannot execute`` at every later invocation — including from
        every git hook — rather than failing at the one moment someone is watching setup.
        """
        path = self.launcher_path()
        if not _wrapper_is_executable(self._repo):
            echo(_message(LauncherInstall.NO_TARGET, path, self._repo))
            return
        outcome = install_launcher(path, self._repo)
        echo(_message(outcome, path, self._repo))
        if outcome in _BLOCKED:
            return
        if not self._resolves_to(path):
            echo(
                f"WARN  `t3` on PATH is not {path} — put that directory ahead of any other "
                f"t3 on PATH, then re-run `t3 setup`. Leaving the uv-installed host t3 in place."
            )
            return
        self._retire_host_tool(echo=echo)

    def _install_through_mount(self, *, echo: Echo) -> None:
        """Write the HOST launcher through the bind mount, for the host checkout in the env.

        Never retires anything: the uv tool registry lives on the host, out of
        reach from here, and the ``t3`` this process resolves is the container's
        own console script, which is the CLI.
        """
        if not self._host_bin_mount.is_dir():
            echo(
                f"OK    No host bin mount at {self._host_bin_mount} — this container was not "
                f"started by deploy/t3, so the host t3 is left alone."
            )
            return
        checkout = self._env.get(DEPLOY_CHECKOUT_ENV, "").strip()
        if not checkout:
            echo(
                f"WARN  The host bin mount at {self._host_bin_mount} is present but "
                f"${DEPLOY_CHECKOUT_ENV} names no host checkout, so the launcher would point "
                f"nowhere — run `t3 setup` through the checkout's `deploy/t3`."
            )
            return
        path = self._host_bin_mount / "t3"
        outcome = install_launcher(path, Path(checkout))
        echo(_message(outcome, path, Path(checkout)))

    def _resolves_to(self, path: Path) -> bool:
        """True when the launcher exists, is executable, and is the ``t3`` PATH finds."""
        if not path.is_file() or not os.access(path, os.X_OK):
            return False
        found = self._which("t3")
        return found is not None and Path(found).resolve() == path.resolve()

    def _retire_host_tool(self, *, echo: Echo) -> None:
        """Remove the uv-installed ``teatree`` tool; never fatal, quiet when absent."""
        uv_bin = self._which("uv")
        if uv_bin is None:
            echo("WARN  `uv` not on PATH — cannot check for a uv-installed host t3 to remove.")
            return
        if not _uv_tool_installed(uv_bin):
            echo("OK    No uv-installed host t3 to remove — the launcher is the only t3.")
            return
        result = run_captured([uv_bin, "tool", "uninstall", "teatree"])
        if result.returncode != 0:
            echo(
                f"WARN  Could not remove the uv-installed host t3: {result.stderr.strip()} — "
                f"remove it with `{uv_bin} tool uninstall teatree`; setup continues."
            )
            return
        echo("OK    Removed the uv-installed host t3 — every t3 now runs in the container.")


def _uv_tool_installed(uv_bin: str) -> bool:
    """True when uv's tool registry holds a ``teatree`` install."""
    result = run_captured([uv_bin, "tool", "dir"])
    if result.returncode != 0 or not result.stdout.strip():
        return False
    return (Path(result.stdout.strip()) / "teatree").is_dir()


#: The ``t3 setup`` line each launcher-install outcome renders, by outcome. A mapping
#: rather than a branch chain so a new outcome is a row, not a seventh return.
_MESSAGES: dict[LauncherInstall, Callable[[Path, Path], str]] = {
    LauncherInstall.INSTALLED: (
        lambda path, repo: f"OK    Installed the containerized t3 launcher at {path} -> {wrapper_path(repo)}."
    ),
    LauncherInstall.UPDATED: (lambda path, repo: f"OK    Repointed the t3 launcher at {path} -> {wrapper_path(repo)}."),
    LauncherInstall.ALREADY_PRESENT: (
        lambda path, _repo: f"OK    Containerized t3 launcher already current at {path}."
    ),
    LauncherInstall.REFUSED: lambda path, _repo: (
        f"WARN  {path} is not a teatree-managed t3 — leaving it untouched. Move it aside "
        f"(`mv {path} {path}.bak`) and re-run `t3 setup` to install the launcher."
    ),
    LauncherInstall.UNVERIFIED: lambda path, repo: (
        f"WARN  Wrote the t3 launcher to {path} but it did not read back as an executable "
        f"launcher for {wrapper_path(repo)} — the previous t3 is untouched; re-run `t3 setup`."
    ),
    LauncherInstall.NO_TARGET: lambda path, repo: (
        f"WARN  {wrapper_path(repo)} is not an executable deploy/t3 from here, so a launcher "
        f"naming it could not exec anything — {path} is untouched. Re-run `t3 setup` from the "
        f"checkout that owns deploy/, or through its `deploy/t3`."
    ),
    LauncherInstall.UNWRITABLE: lambda path, _repo: (
        f"WARN  Could not write the t3 launcher to {path} (not writable) — skipping; setup continues."
    ),
}


def _message(outcome: LauncherInstall, path: Path, repo: Path) -> str:
    """Render the ``t3 setup`` line for a launcher-install *outcome*."""
    return _MESSAGES[outcome](path, repo)

"""Install the containerized-``t3`` shell alias during ``t3 setup`` (#3232).

The headless deployment runs the CLI exclusively in Docker; on a host, a shell
alias points ``t3`` at the container-wrapping entry (``deploy/t3``) so ``t3
<args>`` transparently execs into the running worker. This installer writes a
marker-delimited managed block into the operator's shell rc files (idempotent —
see :func:`teatree.docker.workflow.install_alias_block`).

No-ops inside a container: there the container *is* the runtime, so the alias
would only shadow the real ``t3`` on PATH. Best-effort — an unwritable rc
degrades to a WARN and never aborts setup. The companion doctor check
(:func:`teatree.cli.doctor.checks_docker._check_docker_workflow_wired`) verifies
the wiring afterwards.
"""

from collections.abc import Callable
from pathlib import Path

from teatree.docker.workflow import AliasInstall, install_alias_block, is_running_in_container


class DockerAliasInstaller:
    """Compose unit: install the containerized-``t3`` alias into shell rc files."""

    def __init__(self, repo: Path, home: Path | None = None) -> None:
        self._repo = repo
        self._home = home if home is not None else Path.home()

    def target_rc_files(self) -> list[Path]:
        """Shell rc files to manage: ``~/.bashrc`` always; ``~/.zshrc`` if present.

        ``~/.bashrc`` is created when absent (bash is the container/base shell);
        ``~/.zshrc`` is only touched when it already exists, so setup never
        conjures a zsh profile on a bash-only box.
        """
        targets = [self._home / ".bashrc"]
        zshrc = self._home / ".zshrc"
        if zshrc.is_file():
            targets.append(zshrc)
        return targets

    def install(self, *, echo: Callable[[str], None]) -> None:
        """Write the managed alias block, echoing the outcome per rc file."""
        if is_running_in_container():
            echo("OK    Containerized runtime — skipping host t3 alias (the container IS the CLI).")
            return
        for rc_path in self.target_rc_files():
            outcome = install_alias_block(rc_path, self._repo)
            echo(_message(outcome, rc_path))


def _message(outcome: AliasInstall, rc_path: Path) -> str:
    """Render the per-rc-file ``t3 setup`` line for an install *outcome*."""
    if outcome is AliasInstall.INSTALLED:
        return f"OK    Installed containerized t3 alias into {rc_path} (run `source {rc_path}` or open a new shell)."
    if outcome is AliasInstall.UPDATED:
        return f"OK    Refreshed containerized t3 alias in {rc_path} (path changed)."
    if outcome is AliasInstall.ALREADY_PRESENT:
        return f"OK    Containerized t3 alias already current in {rc_path}."
    return f"WARN  Could not write the containerized t3 alias to {rc_path} (not writable) — skipping; setup continues."

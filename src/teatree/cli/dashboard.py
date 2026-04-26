"""``t3 dashboard`` — start the dashboard dev server with optional background workers."""

import contextlib
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.utils.run import Popen


class DashboardGuard:
    """Singleton guard for the dashboard server using a PID file."""

    def __init__(self, pid_file: Path) -> None:
        self._pid_file = pid_file

    def _read_pid(self) -> int | None:
        try:
            pid = int(self._pid_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            self._pid_file.unlink(missing_ok=True)
            return None
        return pid

    def stop_existing(self) -> bool:
        """Kill an existing dashboard process. Returns True if one was stopped."""
        pid = self._read_pid()
        if pid is None:
            return False
        typer.echo(f"Stopping existing dashboard (PID {pid})...")
        os.kill(pid, signal.SIGTERM)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
        self._pid_file.unlink(missing_ok=True)
        return True

    def write_pid(self) -> None:
        self._pid_file.parent.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(str(os.getpid()))

    def cleanup(self) -> None:
        self._pid_file.unlink(missing_ok=True)


def _resolve_overlay_for_server(*, project: Path | None = None) -> tuple[Path, str, str]:
    """Resolve the project path, overlay name, and settings module.

    When *project* is provided it is used as-is — no auto-detection.  Without
    it, auto-detection succeeds only when the result is unambiguous (exactly
    one entry-point overlay, or CWD inside a single project root).  Ambiguous
    situations abort with a clear error asking for ``--project``.
    """
    from teatree.config import discover_active_overlay, discover_overlays  # noqa: PLC0415

    installed = discover_overlays()
    ep_overlays = [e for e in installed if ":" in (e.overlay_class or "")]

    if project is not None:
        project_path = project.resolve()
        if not (project_path / "pyproject.toml").is_file():
            typer.echo(f"--project {project_path} does not contain pyproject.toml")
            raise typer.Exit(code=1)
        active = ep_overlays[0] if len(ep_overlays) == 1 else discover_active_overlay()
        overlay_name = active.name if active else ""
        settings_module = "teatree.settings"
        if active and ":" not in (active.overlay_class or "") and active.overlay_class:
            settings_module = active.overlay_class
        return project_path, overlay_name, settings_module

    if len(ep_overlays) > 1:
        names = ", ".join(e.name for e in ep_overlays)
        typer.echo(
            f"Multiple overlays installed ({names}). Pass --project <path> to specify which source tree to serve."
        )
        raise typer.Exit(code=1)

    active = ep_overlays[0] if ep_overlays else discover_active_overlay()
    if not active:
        typer.echo("No overlay found. Add one to ~/.teatree.toml or pass --project <path>.")
        raise typer.Exit(code=1)

    project_path = active.project_path
    if not project_path:
        typer.echo(
            f"Overlay '{active.name}' has no project_path configured. Pass --project <path> to specify the source tree."
        )
        raise typer.Exit(code=1)

    overlay_name = active.name
    settings_module = "teatree.settings"
    if ":" not in (active.overlay_class or "") and active.overlay_class:
        settings_module = active.overlay_class
    return project_path, overlay_name, settings_module


def dashboard(
    host: str = typer.Option("127.0.0.1", help="Host to bind to"),
    port: int = typer.Option(8000, help="Port to serve on"),
    *,
    project: Path | None = typer.Option(None, help="Project root to serve from (worktree path)."),
    workers: int = typer.Option(1, help="Number of background task workers to start (0 to disable)"),
    stop: bool = typer.Option(False, "--stop", help="Stop the running dashboard and exit."),
) -> None:
    """Migrate the database and start the dashboard dev server."""
    import socket  # noqa: PLC0415

    from teatree.cli.overlay import _uvicorn, managepy, uv_cmd  # noqa: PLC0415
    from teatree.config import DATA_DIR  # noqa: PLC0415
    from teatree.utils.run import spawn  # noqa: PLC0415

    guard = DashboardGuard(DATA_DIR / "dashboard.pid")

    if stop:
        if guard.stop_existing():
            typer.echo("Dashboard stopped.")
        else:
            typer.echo("No running dashboard found.")
        return

    guard.stop_existing()

    project_path, overlay_name, settings_module = _resolve_overlay_for_server(project=project)
    managepy(project_path, "migrate", "--no-input", overlay_name=overlay_name)
    actual_port = port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex((host, port)) == 0:
            s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s2.bind((host, 0))
            actual_port = s2.getsockname()[1]
            s2.close()
            typer.echo(f"Port {port} in use, using {actual_port}")

    guard.write_pid()

    worker_procs: list[Popen[str]] = []
    if workers > 0 and project_path:
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
        if overlay_name:
            env["T3_OVERLAY_NAME"] = overlay_name
        manage_py = str(project_path / "manage.py")
        worker_cmd = [
            *uv_cmd(project_path, "python", manage_py, "db_worker"),
            "--interval",
            "1",
            "--no-startup-delay",
            "--no-reload",
        ]
        worker_procs.extend(spawn(worker_cmd, cwd=project_path, env=env) for _ in range(workers))
        typer.echo(f"Started {workers} background worker(s).")

    try:
        _uvicorn(project_path, host, actual_port, settings_module, overlay_name=overlay_name)
    finally:
        for p in worker_procs:
            p.terminate()
        for p in worker_procs:
            p.wait(timeout=5)
        guard.cleanup()

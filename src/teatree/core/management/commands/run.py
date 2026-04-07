import os
import subprocess  # noqa: S404
import urllib.request
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

import typer
from django_typer.management import TyperCommand, command

from teatree.core.management.commands.lifecycle import _compose_project
from teatree.core.overlay import RunCommand, RunCommands
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.utils.ports import find_free_ports, get_worktree_ports


def _compose_has_service(compose_file: str, service: str) -> bool:
    """Check if a service is defined in docker-compose (uses ``docker compose config``)."""
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", compose_file, "config", "--services"],
        capture_output=True,
        text=True,
        check=False,
    )
    return service in result.stdout.splitlines()


class Command(TyperCommand):
    @command()
    def verify(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> dict[str, object]:
        """Check that dev services respond via HTTP, then advance FSM.

        Discovers ports from running docker-compose containers via
        ``docker compose port``.
        """
        worktree = resolve_worktree(path)
        project = _compose_project(worktree)
        ports = get_worktree_ports(project)
        results: dict[str, dict[str, object]] = {}

        overlay = get_overlay()
        health_paths = dict(overlay.get_verify_endpoints(worktree))
        # Merge T3_HEALTH_ENDPOINTS env var (format: "service:path,service:path")
        for entry in os.environ.get("T3_HEALTH_ENDPOINTS", "").split(","):
            if ":" in entry:
                svc, path = entry.split(":", 1)
                health_paths[svc.strip()] = path.strip()
        endpoints = {
            name: f"http://localhost:{port}{health_paths.get(name, '/')}"
            for name, port in ports.items()
            if name not in {"postgres", "redis"}
        }

        all_ok = True
        for name, url in endpoints.items():
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
                    results[name] = {"url": url, "status": resp.status, "ok": True}
                    self.stdout.write(f"  {name}: {url} → {resp.status}")
            except Exception as exc:  # noqa: BLE001
                results[name] = {"url": url, "status": 0, "ok": False, "error": str(exc)}
                self.stderr.write(f"  {name}: {url} → FAILED ({exc})")
                all_ok = False

        if all_ok and endpoints:
            urls = {
                name: f"http://localhost:{port}" for name, port in ports.items() if name not in {"postgres", "redis"}
            }
            worktree.verify(urls=urls)
            worktree.save()

        extra = cast("dict[str, object]", worktree.extra or {})
        return {
            "state": worktree.state,
            "urls": extra.get("urls", {}),
            "checks": results,
        }

    @command()
    def services(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> RunCommands:
        worktree = resolve_worktree(path)
        return get_overlay().get_run_commands(worktree)

    @command()
    def backend(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Start the backend via docker-compose."""
        worktree = resolve_worktree(path)
        project = _compose_project(worktree)
        overlay = get_overlay()
        compose_file = overlay.get_compose_file(worktree)
        if not compose_file:
            return "No docker-compose file found."

        from teatree.config import load_config  # noqa: PLC0415
        from teatree.core.management.commands.lifecycle import _compose_env  # noqa: PLC0415

        ports = find_free_ports(str(load_config().user.workspace_dir))
        env = {**os.environ, **overlay.get_env_extra(worktree), **_compose_env(ports)}
        env.pop("VIRTUAL_ENV", None)

        cmd = ["docker", "compose", "-p", project, "-f", compose_file, "up", "-d", "web"]
        subprocess.run(cmd, env=env, check=False)  # noqa: S603
        return "Backend started via docker-compose."

    @command()
    def frontend(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Start the frontend (docker-compose if available, otherwise local)."""
        worktree = resolve_worktree(path)
        overlay = get_overlay()

        # Check if "frontend" service exists in docker-compose.
        compose_file = overlay.get_compose_file(worktree)
        if compose_file and _compose_has_service(compose_file, "frontend"):
            from teatree.config import load_config  # noqa: PLC0415
            from teatree.core.management.commands.lifecycle import _compose_env  # noqa: PLC0415

            ports = find_free_ports(str(load_config().user.workspace_dir))
            env = {**os.environ, **overlay.get_env_extra(worktree), **_compose_env(ports)}
            env.pop("VIRTUAL_ENV", None)

            project = _compose_project(worktree)
            cmd = ["docker", "compose", "-p", project, "-f", compose_file, "up", "-d", "frontend"]
            subprocess.run(cmd, env=env, check=False)  # noqa: S603
            return "Frontend started via docker-compose."

        # Fall back to overlay's local run command.
        commands = overlay.get_run_commands(worktree)
        run_cmd = commands.get("frontend")
        if not run_cmd:
            return "No frontend command configured in overlay or docker-compose."
        args = run_cmd.args if isinstance(run_cmd, RunCommand) else list(run_cmd)
        cwd = run_cmd.cwd if isinstance(run_cmd, RunCommand) else None
        self.stdout.write(f"  Starting frontend locally: {' '.join(args)}")
        if cwd:
            self.stdout.write(f"  cwd: {cwd}")
        subprocess.Popen(args, cwd=cwd, env={**os.environ, **overlay.get_env_extra(worktree)})  # noqa: S603
        return "Frontend started locally (background process)."

    @command(name="build-frontend")
    def build_frontend(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Build the frontend app for production/testing."""
        worktree = resolve_worktree(path)
        commands = get_overlay().get_run_commands(worktree)
        cmd = commands.get("build-frontend", [])
        if not cmd:
            return "No build-frontend command configured in the overlay."
        run_args = cmd.args if isinstance(cmd, RunCommand) else list(cmd)
        subprocess.run(run_args, check=True)  # noqa: S603
        return "Frontend built."

    @command(context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
    def tests(
        self,
        ctx: typer.Context,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Run the project test suite.

        Extra arguments after ``--`` are appended to the test command
        (e.g. ``t3 <overlay> run tests -- path/to/test.py -k name``).
        """
        worktree = resolve_worktree(path)
        overlay = get_overlay()
        test_cmd = overlay.get_test_command(worktree)
        if not test_cmd:
            return "No test command configured in the overlay."

        if isinstance(test_cmd, RunCommand):
            args = list(test_cmd.args)
            cwd: Path | str | None = test_cmd.cwd
        else:
            args = list(test_cmd)
            cwd = None

        args.extend(ctx.args)
        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)

        result = subprocess.run(args, cwd=cwd, env=env, check=False)  # noqa: S603
        if result.returncode != 0:
            return f"Tests failed (exit {result.returncode})."
        return "Tests completed."

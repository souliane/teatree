import os
import urllib.request
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

import typer
from django_typer.management import TyperCommand, command

from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.core.runners.service_launch import ServiceLauncher
from teatree.core.worktree_env import compose_project
from teatree.types import RunCommand, RunCommands
from teatree.utils.ports import get_worktree_ports
from teatree.utils.run import run_streamed


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
        project = compose_project(worktree)
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
                with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — fixed http://localhost URL built from local ports
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
        """Start the backend via docker-compose. Host port is auto-mapped."""
        worktree = resolve_worktree(path)
        project = compose_project(worktree)
        overlay = get_overlay()
        compose_file = overlay.get_compose_file(worktree)
        if not compose_file:
            return "No docker-compose file found."

        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)

        cmd = ["docker", "compose", "-p", project, "-f", compose_file, "up", "-d", "web"]
        run_streamed(cmd, env=env, check=False)
        return "Backend started via docker-compose."

    @command(name="build-frontend")
    def build_frontend(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Build the frontend app for production/testing."""
        return ServiceLauncher(resolve_worktree(path), "build-frontend").run().detail

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
            self.stderr.write("No test command configured in the overlay.")
            raise SystemExit(1)

        if isinstance(test_cmd, RunCommand):
            args = list(test_cmd.args)
            cwd: Path | str | None = test_cmd.cwd
        else:
            args = list(test_cmd)
            cwd = None

        args.extend(ctx.args)
        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)

        rc = run_streamed(args, cwd=cwd, env=env, check=False)
        if rc != 0:
            self.stderr.write(f"Tests failed (exit {rc}).")
            raise SystemExit(1)
        return "Tests completed."

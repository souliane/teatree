import os
import subprocess  # noqa: S404
import urllib.request
from typing import cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.models import Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.core.worktree_env import write_env_worktree


def _run_env(worktree: Worktree) -> dict[str, str]:
    """Build subprocess env from current env + overlay's get_env_extra."""
    worktree.refresh_ports_if_needed()
    write_env_worktree(worktree)
    env = dict(os.environ)
    env.update(get_overlay().get_env_extra(worktree))
    # Remove VIRTUAL_ENV so uv/python uses the worktree's .venv (symlinked to main repo)
    env.pop("VIRTUAL_ENV", None)
    return env


class Command(TyperCommand):
    def _start_services(self, worktree: Worktree) -> None:
        """Ensure Docker services (DB, Redis) are running before starting the app."""
        overlay = get_overlay()
        services = overlay.get_services_config(worktree)
        for name, spec in services.items():
            start_cmd = spec.get("start_command", "")
            if start_cmd:
                self.stdout.write(f"  Starting {name}...")
                subprocess.run(start_cmd, shell=True, check=False, capture_output=True)  # noqa: S602

    @command()
    def verify(
        self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")
    ) -> dict[str, object]:
        """Check that dev services respond via HTTP, then advance FSM."""
        worktree = resolve_worktree(path)
        ports = worktree.ports or {}
        results: dict[str, dict[str, object]] = {}

        endpoints = {
            name: f"http://localhost:{port}" for name, port in ports.items() if name not in {"postgres", "redis"}
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
            worktree.verify()
            worktree.save()

        extra = cast("dict[str, object]", worktree.extra or {})
        return {
            "state": worktree.state,
            "urls": extra.get("urls", {}),
            "checks": results,
        }

    @command()
    def services(
        self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")
    ) -> dict[str, str]:
        worktree = resolve_worktree(path)
        return get_overlay().get_run_commands(worktree)

    def _run_pre_steps(self, worktree: Worktree, service: str) -> None:
        """Execute overlay pre-run steps for *service*."""
        for step in get_overlay().get_pre_run_steps(worktree, service):
            self.stdout.write(f"  Preparing: {step.name}")
            step.callable()

    @command()
    def backend(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Start the backend dev server."""
        worktree = resolve_worktree(path)
        self._start_services(worktree)
        self._run_pre_steps(worktree, "backend")
        commands = get_overlay().get_run_commands(worktree)
        cmd = commands.get("backend", "")
        if not cmd:
            return "No backend command configured in the overlay."
        env = _run_env(worktree)
        subprocess.run(cmd, shell=True, check=True, env=env)  # noqa: S602
        return "Backend started."

    @command()
    def frontend(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Start the frontend dev server."""
        worktree = resolve_worktree(path)
        self._start_services(worktree)
        self._run_pre_steps(worktree, "frontend")
        commands = get_overlay().get_run_commands(worktree)
        cmd = commands.get("frontend", "")
        if not cmd:
            return "No frontend command configured in the overlay."
        env = _run_env(worktree)
        subprocess.run(cmd, shell=True, check=True, env=env)  # noqa: S602
        return "Frontend started."

    @command(name="build-frontend")
    def build_frontend(
        self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")
    ) -> str:
        """Build the frontend app for production/testing."""
        worktree = resolve_worktree(path)
        self._run_pre_steps(worktree, "build-frontend")
        commands = get_overlay().get_run_commands(worktree)
        cmd = commands.get("build-frontend", "")
        if not cmd:
            return "No build-frontend command configured in the overlay."
        subprocess.run(cmd, shell=True, check=True)  # noqa: S602
        return "Frontend built."

    @command()
    def tests(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Run the project test suite."""
        worktree = resolve_worktree(path)
        cmd = get_overlay().get_test_command(worktree)
        if not cmd:
            return "No test command configured in the overlay."
        subprocess.run(cmd, shell=True, check=True)  # noqa: S602
        return "Tests completed."

    @command()
    def e2e(self, branch: str = "") -> dict[str, object]:
        """Run E2E tests (triggers via CI or overlay config)."""
        from teatree.backends.loader import get_ci_service  # noqa: PLC0415

        overlay = get_overlay()
        config = overlay.get_e2e_config()
        if not config:
            return {"error": "No E2E config in the overlay (get_e2e_config)."}

        ci = get_ci_service()
        if ci is None:
            return {"error": "No CI service configured."}

        project = config.get("project_path", overlay.get_ci_project_path())
        ref = branch or config.get("ref", "main")
        variables = {"E2E": "true"}
        return ci.trigger_pipeline(project=project, ref=ref, variables=variables)

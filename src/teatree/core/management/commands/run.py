import os
import subprocess  # noqa: S404
import urllib.request
from pathlib import Path
from typing import cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.models import Worktree
from teatree.core.overlay import RunCommand, RunCommands
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.core.worktree_env import write_env_worktree


def _find_compose_file(wt_path: str, filename: str) -> Path | None:
    """Locate a docker-compose file in the dev/ directory."""
    for base in (Path(__file__).resolve().parents[4], Path(wt_path)):
        candidate = base / "dev" / filename
        if candidate.is_file():
            return candidate
    return None


def _run_env(worktree: Worktree) -> dict[str, str]:
    """Build subprocess env from current env + overlay's get_env_extra.

    Sets ``VIRTUAL_ENV`` to the worktree's ``.venv`` so that ``uv run``
    and bare ``python`` resolve to the correct interpreter.  Falls back
    to stripping ``VIRTUAL_ENV`` if the worktree has no ``.venv``.
    """
    worktree.refresh_ports_if_needed()
    write_env_worktree(worktree)
    env = dict(os.environ)
    env.update(get_overlay().get_env_extra(worktree))

    wt_path = (worktree.extra or {}).get("worktree_path", "")
    venv = Path(wt_path) / ".venv" if wt_path else None
    if venv and venv.is_dir():
        env["VIRTUAL_ENV"] = str(venv)
    else:
        env.pop("VIRTUAL_ENV", None)
    return env


class Command(TyperCommand):
    def _start_services(self, worktree: Worktree) -> None:
        """Ensure Docker services (DB, Redis) are running before starting the app."""
        overlay = get_overlay()
        services = overlay.get_services_config(worktree)
        for name, spec in services.items():
            start_cmd = spec.get("start_command", [])
            if start_cmd:
                self.stdout.write(f"  Starting {name}...")
                subprocess.run(start_cmd, check=False, capture_output=True)  # noqa: S603

    @command()
    def verify(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> dict[str, object]:
        """Check that dev services respond via HTTP, then advance FSM."""
        worktree = resolve_worktree(path)
        ports = worktree.ports or {}
        results: dict[str, dict[str, object]] = {}

        overlay = get_overlay()
        health_paths = overlay.get_verify_endpoints(worktree)
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
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> RunCommands:
        worktree = resolve_worktree(path)
        return get_overlay().get_run_commands(worktree)

    def _run_pre_steps(self, worktree: Worktree, service: str) -> None:
        """Execute overlay pre-run steps for *service*."""
        for step in get_overlay().get_pre_run_steps(worktree, service):
            self.stdout.write(f"  Preparing: {step.name}")
            step.callable()

    def _run_command(self, cmd: list[str] | RunCommand, env: dict[str, str] | None = None) -> None:
        if isinstance(cmd, RunCommand):
            subprocess.run(cmd.args, check=True, env=env, cwd=cmd.cwd)  # noqa: S603
        else:
            subprocess.run(cmd, check=True, env=env)  # noqa: S603

    @command()
    def backend(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Start the backend dev server."""
        worktree = resolve_worktree(path)
        self._start_services(worktree)
        self._run_pre_steps(worktree, "backend")
        commands = get_overlay().get_run_commands(worktree)
        cmd = commands.get("backend", [])
        if not cmd:
            return "No backend command configured in the overlay."
        self._run_command(cmd, env=_run_env(worktree))
        return "Backend started."

    @command()
    def frontend(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Start the frontend dev server."""
        worktree = resolve_worktree(path)
        self._start_services(worktree)
        self._run_pre_steps(worktree, "frontend")
        commands = get_overlay().get_run_commands(worktree)
        cmd = commands.get("frontend", [])
        if not cmd:
            return "No frontend command configured in the overlay."
        self._run_command(cmd, env=_run_env(worktree))
        return "Frontend started."

    @command(name="build-frontend")
    def build_frontend(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Build the frontend app for production/testing."""
        worktree = resolve_worktree(path)
        self._run_pre_steps(worktree, "build-frontend")
        commands = get_overlay().get_run_commands(worktree)
        cmd = commands.get("build-frontend", [])
        if not cmd:
            return "No build-frontend command configured in the overlay."
        self._run_command(cmd)
        return "Frontend built."

    @command()
    def tests(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Run the project test suite."""
        worktree = resolve_worktree(path)
        cmd = get_overlay().get_test_command(worktree)
        if not cmd:
            return "No test command configured in the overlay."
        subprocess.run(cmd, check=True)  # noqa: S603
        return "Tests completed."

    @command()
    def e2e(self, branch: str = "") -> dict[str, object]:
        """Run E2E tests (triggers via CI or overlay config)."""
        from teatree.backends.loader import get_ci_service  # noqa: PLC0415

        overlay = get_overlay()
        config = overlay.metadata.get_e2e_config()
        if not config:
            return {"error": "No E2E config in the overlay (get_e2e_config)."}

        ci = get_ci_service()
        if ci is None:
            return {"error": "No CI service configured."}

        project = config.get("project_path", overlay.metadata.get_ci_project_path())
        ref = branch or config.get("ref", "main")
        variables = {"E2E": "true"}
        return ci.trigger_pipeline(project=project, ref=ref, variables=variables)

    @command(name="e2e-local")
    def e2e_local(
        self,
        test_path: str = "",
        *,
        headed: bool = False,
        docker: bool = True,
    ) -> str:
        """Run E2E tests locally with Playwright.

        By default runs via docker-compose (``dev/docker-compose.yml``) for
        reproducibility.  Pass ``--no-docker`` to run directly on the host.
        """
        worktree = resolve_worktree()
        overlay = get_overlay()
        wt_path = (worktree.extra or {}).get("worktree_path", ".") if worktree else "."
        e2e_config = overlay.metadata.get_e2e_config()
        settings_module = e2e_config.get("settings_module", "e2e.settings")
        test_dir = test_path or e2e_config.get("test_dir", "e2e/")

        if docker:
            compose_file = _find_compose_file(wt_path, "docker-compose.yml")
            if compose_file:
                cmd = ["docker", "compose", "-f", str(compose_file), "run", "--rm", "e2e"]
                result = subprocess.run(cmd, cwd=wt_path, check=False)  # noqa: S603
                return "E2E passed." if result.returncode == 0 else f"E2E failed (exit {result.returncode})."

        cmd = ["uv", "run", "--group", "e2e", "pytest", test_dir]
        cmd.extend(["--ds", settings_module, "--no-cov", "-n", "auto", "-v"])

        env = {**os.environ, "DJANGO_SETTINGS_MODULE": settings_module}
        if headed:
            env.pop("CI", None)
        else:
            env["CI"] = "1"

        result = subprocess.run(cmd, cwd=wt_path, check=False, env=env)  # noqa: S603
        return "E2E passed." if result.returncode == 0 else f"E2E failed (exit {result.returncode})."

    @command(name="e2e-private")
    def e2e_private(self, test_path: str = "", *, headed: bool = False) -> str:
        """Run private Playwright tests from T3_PRIVATE_TESTS repo."""
        from teatree.config import load_config  # noqa: PLC0415

        private_tests = os.environ.get("T3_PRIVATE_TESTS", "")
        if not private_tests:
            private_tests = load_config().raw.get("teatree", {}).get("private_tests", "")
        if not private_tests:
            return "private_tests not configured in ~/.teatree.toml and T3_PRIVATE_TESTS not set."
        private_tests_path = Path(private_tests).expanduser()
        if not private_tests_path.is_dir():
            return f"Private tests directory does not exist: {private_tests_path}"

        worktree = resolve_worktree()
        ports = worktree.ports or {} if worktree else {}
        frontend_port = ports.get("frontend", 4200)

        cmd = ["npx", "playwright", "test"]
        if test_path:
            cmd.append(test_path)
        cmd.extend(["--reporter=list"])

        env = {**os.environ}
        env["BASE_URL"] = f"http://localhost:{frontend_port}"
        if headed:
            env.pop("CI", None)
            cmd.append("--headed")
        else:
            env["CI"] = "1"

        self.stdout.write(f"  Running from: {private_tests_path}")
        self.stdout.write(f"  BASE_URL: {env['BASE_URL']}")

        result = subprocess.run(cmd, cwd=private_tests_path, check=False, env=env)  # noqa: S603
        return "E2E passed." if result.returncode == 0 else f"E2E failed (exit {result.returncode})."

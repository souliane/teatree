"""E2E test commands: trigger CI, run from external repo, run from project."""

import os
import socket
import subprocess  # noqa: S404
from pathlib import Path

from django_typer.management import TyperCommand, command

from teatree.core.management.commands.lifecycle import _compose_project
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.utils.ports import get_service_port


def _detect_local_port(port: int) -> int | None:
    """Return *port* if something is listening on localhost, else None."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return port
    return None


def _resolve_private_tests_path() -> Path | None:
    """Resolve the private tests directory from env or config."""
    from teatree.config import load_config  # noqa: PLC0415

    private_tests = os.environ.get("T3_PRIVATE_TESTS", "")
    if not private_tests:
        private_tests = load_config().raw.get("teatree", {}).get("private_tests", "")
    if not private_tests:
        return None
    path = Path(private_tests).expanduser()
    return path if path.is_dir() else None


def _discover_frontend_port(project: str, default: int = 4200) -> int | None:
    """Try docker-compose service, then fall back to local port check."""
    port = get_service_port(project, "frontend", default)
    if port is not None:
        return port
    return _detect_local_port(default)


class Command(TyperCommand):
    @command(name="trigger-ci")
    def trigger_ci(self, branch: str = "") -> dict[str, object]:
        """Trigger E2E tests on a remote CI pipeline."""
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

    @command()
    def external(self, test_path: str = "", *, headed: bool = False) -> str:
        """Run Playwright tests from the external test repo (T3_PRIVATE_TESTS).

        Discovers the frontend port from docker-compose (or local process)
        and reads the tenant variant from .env.worktree.
        """
        private_tests_path = _resolve_private_tests_path()
        if not private_tests_path:
            return "private_tests not configured in ~/.teatree.toml / T3_PRIVATE_TESTS, or directory missing."

        worktree = resolve_worktree()
        project = _compose_project(worktree)
        frontend_port = _discover_frontend_port(project)
        if frontend_port is None:
            return (
                f"Frontend not running (no docker service in '{project}', no local process on 4200). "
                "Run `t3 run frontend` first."
            )

        from teatree.core.resolve import _find_env_worktree, _get_user_cwd, _parse_env_file  # noqa: PLC0415

        variant = ""
        envfile = _find_env_worktree(_get_user_cwd())
        if envfile is not None:
            wt_env = _parse_env_file(envfile)
            variant = wt_env.get("WT_VARIANT", "")

        cmd = ["npx", "playwright", "test"]
        if test_path:
            cmd.append(test_path)
        cmd.extend(["--reporter=list"])

        env = {**os.environ}
        env["BASE_URL"] = f"http://localhost:{frontend_port}"
        if variant:
            env["CUSTOMER"] = variant
        if headed:
            env.pop("CI", None)
            cmd.append("--headed")
        else:
            env["CI"] = "1"

        self.stdout.write(f"  Running from: {private_tests_path}")
        self.stdout.write(f"  BASE_URL: {env['BASE_URL']}")
        if variant:
            self.stdout.write(f"  CUSTOMER: {variant}")

        result = subprocess.run(cmd, cwd=private_tests_path, check=False, env=env)  # noqa: S603
        return "E2E passed." if result.returncode == 0 else f"E2E failed (exit {result.returncode})."

    @command()
    def project(self, test_path: str = "", *, headed: bool = False, docker: bool = True) -> str:
        """Run E2E tests from the project's own test directory."""
        try:
            worktree = resolve_worktree()
            wt_path = (worktree.extra or {}).get("worktree_path", ".") if worktree else "."
        except Exception:  # noqa: BLE001
            wt_path = "."
        overlay = get_overlay()
        e2e_config = overlay.metadata.get_e2e_config()
        settings_module = e2e_config.get("settings_module", "e2e.settings")
        test_dir = test_path or e2e_config.get("test_dir", "e2e/")

        if docker and not Path("/.dockerenv").exists():
            compose_file = Path(wt_path) / "dev" / "docker-compose.yml"
            if compose_file.is_file():
                cmd = ["docker", "compose", "-f", str(compose_file), "run", "--rm", "e2e"]
                result = subprocess.run(cmd, cwd=wt_path, check=False)  # noqa: S603
                return "E2E passed." if result.returncode == 0 else f"E2E failed (exit {result.returncode})."

        cmd = ["uv", "run", "pytest", test_dir]
        cmd.extend(["-o", f"DJANGO_SETTINGS_MODULE={settings_module}", "--no-cov", "-p", "no:tach", "-v"])

        env = {**os.environ, "DJANGO_SETTINGS_MODULE": settings_module}
        if headed:
            env.pop("CI", None)
        else:
            env["CI"] = "1"

        result = subprocess.run(cmd, cwd=wt_path, check=False, env=env)  # noqa: S603
        return "E2E passed." if result.returncode == 0 else f"E2E failed (exit {result.returncode})."

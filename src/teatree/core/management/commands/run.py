import os
import re
import socket
import subprocess  # noqa: S404
import urllib.request
from pathlib import Path
from typing import cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.management.commands.lifecycle import _compose_project
from teatree.core.overlay import RunCommand, RunCommands
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.utils.ports import find_free_ports, get_service_port, get_worktree_ports


def _compose_has_service(compose_file: str, service: str) -> bool:
    """Check if a service is defined in docker-compose (uses ``docker compose config``)."""
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", compose_file, "config", "--services"],
        capture_output=True,
        text=True,
        check=False,
    )
    return service in result.stdout.splitlines()


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


def _detect_nx_serve_port(worktree_path: str) -> int | None:
    """Find a running ``nx serve`` whose cwd matches *worktree_path* and extract ``--port``."""
    result = subprocess.run(
        ["ps", "axo", "args"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        if "nx serve" not in line or "--port=" not in line:
            continue
        if worktree_path not in line:
            continue
        match = re.search(r"--port=(\d+)", line)
        if match:
            return int(match.group(1))
    return None


def _discover_frontend_port(project: str, default: int = 4200) -> int | None:
    """Try nx serve process match, then docker-compose, then local port check."""
    from teatree.core.resolve import _find_env_worktree, _get_user_cwd  # noqa: PLC0415

    cwd = _get_user_cwd()
    envfile = _find_env_worktree(cwd)
    if envfile is not None:
        worktree_root = str(envfile.parent)
        nx_port = _detect_nx_serve_port(worktree_root)
        if nx_port is not None:
            return nx_port
    port = get_service_port(project, "frontend", default)
    if port is not None:
        return port
    return _detect_local_port(default)


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
        """Run E2E tests locally with Playwright."""
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

    @command(name="e2e-private")
    def e2e_private(self, test_path: str = "", *, headed: bool = False) -> str:
        """Run private Playwright tests from T3_PRIVATE_TESTS repo.

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

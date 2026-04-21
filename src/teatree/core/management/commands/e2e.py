"""E2E test commands: trigger CI, run from external repo, run from project."""

import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

from django_typer.management import TyperCommand, command

from teatree.config import E2ERepo, get_data_dir, load_e2e_repos
from teatree.core.management.commands.lifecycle import compose_project
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import _find_env_worktree, _get_user_cwd, _parse_env_file, resolve_worktree
from teatree.utils.ports import get_service_port
from teatree.utils.run import run_allowed_to_fail, run_checked, run_streamed


@dataclass
class PlaywrightOptions:
    """Flags forwarded to the Playwright CLI."""

    test_path: str = ""
    update_snapshots: bool = False
    headed: bool = False
    extra: list[str] = field(default_factory=list)

    def to_args(self) -> list[str]:
        args: list[str] = []
        if self.test_path:
            args.append(self.test_path)
        args.append("--reporter=list")
        if self.update_snapshots:
            args.append("--update-snapshots")
        if self.headed:
            args.append("--headed")
        args.extend(self.extra)
        return args


def _detect_local_port(port: int) -> int | None:
    """Return *port* if something is listening on localhost, else None."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return port
    return None


def _detect_nx_serve_port(worktree_path: str) -> int | None:
    """Find a running ``nx serve`` whose args contain *worktree_path* and extract ``--port``.

    Prevents multi-worktree port collisions: a plain port scan finds the first
    listening process and can pick up another worktree's frontend. Matching by
    worktree path ensures we discover the right ``nx serve`` instance.
    """
    result = run_allowed_to_fail(["ps", "axo", "args"], expected_codes=None)
    for line in result.stdout.splitlines():
        if "nx serve" not in line or "--port=" not in line:
            continue
        if worktree_path not in line:
            continue
        match = re.search(r"--port=(\d+)", line)
        if match:
            return int(match.group(1))
    return None


def _clone_or_update_e2e_repo(repo: E2ERepo) -> Path:
    """Clone or update an external E2E repo to the local cache and return the playwright root.

    On first run: ``git clone --branch <branch> --depth 1 <url> <cache_path>``.
    On subsequent runs: ``git fetch origin <branch>`` + ``git reset --hard FETCH_HEAD``.

    Returns ``cache_path / repo.e2e_dir`` — the directory passed as ``cwd`` to Playwright.
    """
    cache_path = get_data_dir("e2e-repos") / repo.name
    if not cache_path.exists():
        run_checked(
            ["git", "clone", "--branch", repo.branch, "--depth", "1", repo.url, str(cache_path)],
        )
    else:
        run_checked(["git", "-C", str(cache_path), "fetch", "origin", repo.branch])
        run_checked(["git", "-C", str(cache_path), "reset", "--hard", "FETCH_HEAD"])
    return cache_path / repo.e2e_dir


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
    """Discover frontend port: nx serve match → docker-compose → local port scan.

    nx serve process matching is tried first when a worktree env file is found.
    A plain port scan would return the first listening port in the range, which
    in multi-worktree setups can be another worktree's frontend.
    """
    cwd = _get_user_cwd()
    envfile = _find_env_worktree(cwd)
    if envfile is not None:
        nx_port = _detect_nx_serve_port(str(envfile.parent))
        if nx_port is not None:
            return nx_port
    port = get_service_port(project, "frontend", default)
    if port is not None:
        return port
    # Scan the allocation range — ports start at 4200 and go up
    for candidate in range(4200, 4211):
        if _detect_local_port(candidate) is not None:
            return candidate
    return None


def _build_e2e_env(frontend_url: str | None = None, *, headed: bool) -> dict[str, str]:
    """Build environment dict for Playwright: BASE_URL, CUSTOMER, CI.

    When *frontend_url* is given it overrides ``BASE_URL``.
    When it is ``None`` the existing ``BASE_URL`` env var is preserved (DEV / staging mode).
    """
    env = {**os.environ}
    if frontend_url is not None:
        env["BASE_URL"] = frontend_url

    if "CUSTOMER" not in env:
        envfile = _find_env_worktree(_get_user_cwd())
        if envfile is not None:
            variant = _parse_env_file(envfile).get("WT_VARIANT", "")
            if variant:
                env["CUSTOMER"] = variant

    if headed:
        env.pop("CI", None)
    else:
        env["CI"] = "1"
    return env


class Command(TyperCommand):
    @command(name="trigger-ci")
    def trigger_ci(self, branch: str = "") -> dict[str, object]:
        """Trigger E2E tests on a remote CI pipeline."""
        from teatree.core.backend_factory import ci_service_from_overlay  # noqa: PLC0415

        overlay = get_overlay()
        config = overlay.metadata.get_e2e_config()
        if not config:
            return {"error": "No E2E config in the overlay (get_e2e_config)."}

        ci = ci_service_from_overlay()
        if ci is None:
            return {"error": "No CI service configured."}

        project = config.get("project_path", overlay.metadata.get_ci_project_path())
        ref = branch or config.get("ref", "main")
        variables = {"E2E": "true"}
        return ci.trigger_pipeline(project=project, ref=ref, variables=variables)

    @command()
    def external(
        self,
        test_path: str = "",
        *,
        repo: str = "",
        headed: bool = False,
        update_snapshots: bool = False,
        playwright_args: str = "",
    ) -> str:
        """Run Playwright tests from the external test repo (T3_PRIVATE_TESTS or --repo).

        Two sources for the Playwright working directory:

        - ``--repo <name>``: clone/update the named repo from ``[e2e_repos.<name>]`` in
            ``~/.teatree.toml`` and use its ``e2e_dir`` subdirectory.
        - Default: resolve from ``T3_PRIVATE_TESTS`` env var or ``[teatree].private_tests``
            config key.

        Discovers the frontend port from docker-compose (or local process)
        and reads the tenant variant from .env.worktree.

        Extra Playwright flags (--config, --timeout, --grep, etc.) can be
        passed via --playwright-args: ``--playwright-args="--config x.ts --timeout 120000"``
        """
        if repo:
            repos_by_name = {r.name: r for r in load_e2e_repos()}
            if repo not in repos_by_name:
                return f"E2E repo '{repo}' not found in ~/.teatree.toml [e2e_repos]."
            private_tests_path = _clone_or_update_e2e_repo(repos_by_name[repo])
        else:
            private_tests_path = _resolve_private_tests_path()
            if not private_tests_path:
                return "private_tests not configured in ~/.teatree.toml / T3_PRIVATE_TESTS, or directory missing."

        # When BASE_URL is already set (DEV / staging target), skip local port discovery.
        if os.environ.get("BASE_URL"):
            frontend_url = None  # preserve existing BASE_URL
        else:
            worktree = resolve_worktree()
            project = compose_project(worktree)
            frontend_port = _discover_frontend_port(project)
            if frontend_port is None:
                return (
                    f"Frontend not running (no docker service in '{project}', no local process on 4200). "
                    "Run `t3 run frontend` first."
                )
            frontend_url = f"http://localhost:{frontend_port}"

        extra = playwright_args.split() if playwright_args else []
        opts = PlaywrightOptions(
            test_path=test_path,
            update_snapshots=update_snapshots,
            headed=headed,
            extra=extra,
        )
        env = _build_e2e_env(frontend_url, headed=headed)

        self.stdout.write(f"  Running from: {private_tests_path}")
        self.stdout.write(f"  BASE_URL: {env['BASE_URL']}")
        if env.get("CUSTOMER"):
            self.stdout.write(f"  CUSTOMER: {env['CUSTOMER']}")

        cmd = ["npx", "playwright", "test", *opts.to_args()]
        rc = run_streamed(cmd, cwd=private_tests_path, env=env, check=False)
        if rc == 0:
            return "E2E passed."
        self.stderr.write(f"E2E failed (exit {rc}).")
        raise SystemExit(rc)

    @command()
    def project(
        self,
        test_path: str = "",
        *,
        headed: bool = False,
        docker: bool = True,
        update_snapshots: bool = False,
    ) -> str:
        """Run E2E tests from the project's own test directory.

        Pass ``--update-snapshots`` to regenerate ``pytest-playwright-visual``
        baselines. Always do this inside the Docker image (the default) — the
        CI runner's Chromium renders fonts at different heights than macOS, so
        locally-generated baselines mismatch in CI.
        """
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
                if update_snapshots:
                    cmd.append("--update-snapshots")
                rc = run_streamed(cmd, cwd=wt_path, check=False)
                if rc == 0:
                    return "E2E passed."
                self.stderr.write(f"E2E failed (exit {rc}).")
                raise SystemExit(rc)

        cmd = ["uv", "run", "pytest", test_dir]
        cmd.extend(["-o", f"DJANGO_SETTINGS_MODULE={settings_module}", "--no-cov", "-p", "no:tach", "-v"])
        if update_snapshots:
            cmd.append("--update-snapshots")

        env = {**os.environ, "DJANGO_SETTINGS_MODULE": settings_module}
        if headed:
            env.pop("CI", None)
        else:
            env["CI"] = "1"

        rc = run_streamed(cmd, cwd=wt_path, env=env, check=False)
        if rc == 0:
            return "E2E passed."
        self.stderr.write(f"E2E failed (exit {rc}).")
        raise SystemExit(rc)

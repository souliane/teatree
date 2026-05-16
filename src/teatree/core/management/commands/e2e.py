"""E2E test commands: trigger CI, run from external repo, run from project."""

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

from django_typer.management import TyperCommand, command

from teatree.config import E2ERepo, load_e2e_repos
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import _find_env_cache, _get_user_cwd, _parse_env_file, resolve_worktree
from teatree.core.runners.worktree_start import compose_project
from teatree.paths import get_data_dir
from teatree.utils.ports import get_service_port
from teatree.utils.run import run_checked, run_streamed


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
    """Discover frontend port via docker-compose, falling back to a local scan.

    The frontend is served by the compose ``frontend`` service (nginx serving
    the pre-built dist/). ``docker compose port`` is the authoritative answer
    when the stack is up; the local scan is a last-ditch fallback for users
    who started compose outside the teatree runner.
    """
    # The compose `frontend` service is nginx serving the pre-built dist on
    # container port 80; a raw dev-server setup instead listens on 4200.
    # Query both container ports — `default` is the host-scan fallback, not
    # the container port, so it must not be passed to `docker compose port`
    # (that always missed the nginx:80 service).
    for container_port in (80, default):
        port = get_service_port(project, "frontend", container_port)
        if port is not None:
            return port
    # Scan the allocation range — ports start at 4200 and go up
    for candidate in range(4200, 4211):
        if _detect_local_port(candidate) is not None:
            return candidate
    return None


def _build_e2e_env(
    frontend_url: str | None = None,
    *,
    headed: bool,
    target: str,
) -> dict[str, str]:
    """Build environment dict for Playwright: ``BASE_URL``, overlay extras, ``CI``.

    When *frontend_url* is given it overrides ``BASE_URL``.
    When it is ``None`` the existing ``BASE_URL`` env var is preserved (DEV / staging mode).

    *target* is the resolved dual-env target (``"dev"`` or ``"local"``); it is
    exported as ``T3_E2E_TARGET`` so a single dual-mode spec can branch on
    ``process.env.T3_E2E_TARGET === 'dev'`` instead of re-deriving the target
    from a ``BASE_URL`` host regex.

    Overlay-specific env vars (e.g. ``CUSTOMER``) come from
    :meth:`OverlayBase.get_e2e_env_extras` — core only knows about ``BASE_URL``,
    ``T3_E2E_TARGET`` and ``CI``.
    """
    env = {**os.environ}
    if frontend_url is not None:
        env["BASE_URL"] = frontend_url
    env["T3_E2E_TARGET"] = target

    envfile = _find_env_cache(_get_user_cwd())
    env_cache = _parse_env_file(envfile) if envfile is not None else {}
    for key, value in get_overlay().get_e2e_env_extras(env_cache).items():
        env.setdefault(key, value)

    if headed:
        env.pop("CI", None)
    else:
        env["CI"] = "1"
    return env


class Command(TyperCommand):
    @command()
    def run(
        self,
        test_path: str = "",
        *,
        target: str = "",
        headed: bool = False,
        update_snapshots: bool = False,
        docker: bool = True,
    ) -> str:
        """Run E2E tests — the one command that works for every overlay.

        Dispatches to the ``project`` runner (in-repo pytest-playwright) or the
        ``external`` runner (remote playwright repo) based on what the overlay's
        ``get_e2e_config()`` returns. The overlay declares ``"runner": "project"``
        or ``"runner": "external"``; when absent, ``test_dir`` implies ``project``
        and ``project_path`` implies ``external`` for compatibility.

        ``--target dev|local`` selects the dual-env target and is forwarded to
        whichever runner handles the overlay (see ``external`` for semantics).

        Runner-specific flags (``--repo``, ``--playwright-args``) stay on the
        explicit ``external`` subcommand to keep this entry point overlay-agnostic.
        """
        overlay = get_overlay()
        e2e_config = overlay.metadata.get_e2e_config()
        runner = e2e_config.get("runner") or self._infer_runner(e2e_config)
        if runner == "project":
            return self.project(
                test_path=test_path,
                target=target,
                headed=headed,
                docker=docker,
                update_snapshots=update_snapshots,
            )
        if runner == "external":
            return self.external(
                test_path=test_path,
                target=target,
                headed=headed,
                update_snapshots=update_snapshots,
            )
        self.stderr.write(
            f"Overlay e2e_config has no runner ({e2e_config}). "
            "Set 'runner' to 'project' or 'external' in get_e2e_config().",
        )
        raise SystemExit(2)

    @staticmethod
    def _infer_runner(e2e_config: dict[str, str]) -> str:
        if "test_dir" in e2e_config or "settings_module" in e2e_config:
            return "project"
        if "project_path" in e2e_config:
            return "external"
        return ""

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

    def _run_preflight(self, env: dict[str, str]) -> None:
        """Run overlay-declared preflight checks. Exit non-zero on first failure."""
        overlay = get_overlay()
        checks = overlay.get_e2e_preflight(customer=env.get("CUSTOMER") or None, base_url=env.get("BASE_URL") or None)
        for check in checks:
            try:
                check()
            except RuntimeError as exc:
                self.stderr.write(f"E2E preflight failed: {exc}")
                raise SystemExit(1) from exc

    def _resolve_target(self, target: str) -> str:
        """Resolve the dual-env target deterministically.

        ``dev`` / ``local`` are explicit. Empty means back-compat inference:
        a pre-set ``BASE_URL`` env var means a remote target (``dev``),
        otherwise ``local``. The result is exported verbatim as
        ``T3_E2E_TARGET`` so the spec never re-derives it from a host regex.
        """
        normalized = target.strip().lower()
        if normalized in {"dev", "local"}:
            return normalized
        if normalized:
            self.stderr.write(f"--target must be 'dev' or 'local', got {target!r}.")
            raise SystemExit(2)
        return "dev" if os.environ.get("BASE_URL") else "local"

    @command()
    def external(  # noqa: PLR0913
        self,
        test_path: str = "",
        *,
        repo: str = "",
        target: str = "",
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

        ``--target dev|local`` selects the dual-env target deterministically:

        - ``dev``: keep the pre-set ``BASE_URL`` (deployed env), no port scan.
        - ``local``: always discover the local frontend, even if a stray
            ``BASE_URL`` is exported (``--target local`` never hits a
            deployed env silently).
        - empty: back-compat — infer ``dev`` if ``BASE_URL`` is set,
            else ``local``.

        The resolved value is exported as ``T3_E2E_TARGET`` so a dual-mode
        spec branches on ``process.env.T3_E2E_TARGET === 'dev'`` rather than
        re-deriving the target from a ``BASE_URL`` host regex.

        Discovers the frontend port from docker-compose (or local process)
        and reads the tenant variant from the env cache.

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

        resolved_target = self._resolve_target(target)

        # target=dev   → keep the pre-set BASE_URL (deployed env), no port scan.
        # target=local → always discover the local frontend, even if a stray
        #                 BASE_URL is exported, so `--target local` can never
        #                 silently hit a deployed environment.
        if resolved_target == "dev":
            if not os.environ.get("BASE_URL"):
                return "--target dev requires BASE_URL (the deployed environment URL) to be set."
            frontend_url = None  # preserve existing BASE_URL
        else:
            worktree = resolve_worktree()
            project = compose_project(worktree)
            frontend_port = _discover_frontend_port(project)
            if frontend_port is None:
                return (
                    f"Frontend not running (no docker service in '{project}', no local process on 4200). "
                    "Run `t3 <overlay> worktree start` first."
                )
            frontend_url = f"http://localhost:{frontend_port}"

        extra = playwright_args.split() if playwright_args else []
        opts = PlaywrightOptions(
            test_path=test_path,
            update_snapshots=update_snapshots,
            headed=headed,
            extra=extra,
        )
        env = _build_e2e_env(frontend_url, headed=headed, target=resolved_target)

        self.stdout.write(f"  Running from: {private_tests_path}")
        self.stdout.write(f"  Target: {resolved_target}")
        self.stdout.write(f"  BASE_URL: {env['BASE_URL']}")
        if env.get("CUSTOMER"):
            self.stdout.write(f"  CUSTOMER: {env['CUSTOMER']}")

        self._run_preflight(env)

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
        target: str = "",
        headed: bool = False,
        docker: bool = True,
        update_snapshots: bool = False,
    ) -> str:
        """Run E2E tests from the project's own test directory.

        ``--target dev|local`` is exported as ``T3_E2E_TARGET`` for the in-repo
        suite (same contract as the ``external`` runner); empty falls back to
        ``BASE_URL``-based inference.

        Pass ``--update-snapshots`` to regenerate ``pytest-playwright-visual``
        baselines. Always do this inside the Docker image (the default) — the
        CI runner's Chromium renders fonts at different heights than macOS, so
        locally-generated baselines mismatch in CI.
        """
        resolved_target = self._resolve_target(target)
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
                cmd = [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "run",
                    "--rm",
                    "-e",
                    f"T3_E2E_TARGET={resolved_target}",
                    "e2e",
                    test_dir,
                ]
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

        env = {**os.environ, "DJANGO_SETTINGS_MODULE": settings_module, "T3_E2E_TARGET": resolved_target}
        if headed:
            env.pop("CI", None)
        else:
            env["CI"] = "1"

        rc = run_streamed(cmd, cwd=wt_path, env=env, check=False)
        if rc == 0:
            return "E2E passed."
        self.stderr.write(f"E2E failed (exit {rc}).")
        raise SystemExit(rc)

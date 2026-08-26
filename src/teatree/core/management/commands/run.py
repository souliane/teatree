import os
import urllib.request
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

    from teatree.core.models import Worktree

import typer
from django_typer.management import TyperCommand, command

from teatree.core.intake.resolve import resolve_worktree
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.service_launch import ServiceLauncher
from teatree.core.worktree.worktree_env import compose_project
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

        A failed probe exits 1 — the same contract ``workspace ready`` already
        holds. Returning the report at exit 0 told CI and the loop that an
        unreachable service had been verified.
        """
        worktree = resolve_worktree(path)
        project = compose_project(worktree)
        ports = get_worktree_ports(project)
        results: dict[str, dict[str, object]] = {}

        overlay = get_overlay()
        health_paths = dict(overlay.runtime.verify_endpoints(worktree))
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
            except Exception as exc:  # noqa: BLE001 — an endpoint probe failure is recorded as a failed check, never aborts the verify loop
                results[name] = {"url": url, "status": 0, "ok": False, "error": str(exc)}
                self.stderr.write(f"  {name}: {url} → FAILED ({exc})")
                all_ok = False

        if all_ok and endpoints:
            urls = {
                name: f"http://localhost:{port}" for name, port in ports.items() if name not in {"postgres", "redis"}
            }
            worktree.verify(urls=urls)
            worktree.save()

        if not all_ok:
            failed = sum(1 for check in results.values() if not check["ok"])
            self.stderr.write(f"  {failed} of {len(results)} probe(s) failed")
            raise SystemExit(1)

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
        return get_overlay().runtime.run_commands(worktree)

    @command()
    def backend(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Start the backend via docker-compose. Host port is auto-mapped."""
        worktree = resolve_worktree(path)
        project = compose_project(worktree)
        overlay = get_overlay()
        compose_file = overlay.provisioning.compose_file(worktree)
        if not compose_file:
            return "No docker-compose file found."

        env: dict[str, str] = {**os.environ, **overlay.provisioning.env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)

        cmd = ["docker", "compose", "-p", project, "-f", compose_file, "up", "-d", "web"]
        rc = run_streamed(cmd, env=env, check=False)
        if rc != 0:
            self.stderr.write(f"Backend start failed (docker compose up exit {rc}).")
            raise SystemExit(1)
        return "Backend started via docker-compose."

    @command(name="build-frontend")
    def build_frontend(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        *,
        prod: bool = typer.Option(default=False, help="Run the overlay's CI production build."),
    ) -> str:
        """Build the frontend app for production/testing.

        Every ``ok=False`` the launcher reports — no command configured, a build
        already in flight, a non-zero build exit — means nothing was built, so it
        stops the caller with exit 1 exactly as ``run tests`` / ``run lint`` do.
        Discarding ``ok`` here reported a green production-build gate over a build
        that never ran.

        ``--prod`` launches the overlay's ``build-frontend-prod`` command instead,
        so "does the production build pass?" has an answer that needs no raw build
        tool; an overlay declaring no such command fails loud rather than quietly
        building the dev configuration and reporting it as the production one.
        """
        service = "build-frontend-prod" if prod else "build-frontend"
        result = ServiceLauncher(resolve_worktree(path), service).run()
        if not result.ok:
            self.stderr.write(result.detail)
            raise SystemExit(1)
        return result.detail

    @command(context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
    def tests(
        self,
        ctx: typer.Context,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Run the project test suite.

        Extra arguments after ``--`` are appended to the test command
        (e.g. ``t3 <overlay> run tests -- path/to/test.py -k name``).

        The overlay's ``runtime.pre_run_steps(worktree, "tests")`` run first —
        the same prerequisite seam every service launch uses — so an overlay
        can keep its test environment fast and correct (e.g. clone/refresh a
        reusable test DB) without every caller re-deciding the prerequisites.
        """
        worktree = resolve_worktree(path)
        overlay = get_overlay()
        ServiceLauncher(worktree, "tests", overlay=overlay).prepare()
        return self._dispatch_task(
            worktree,
            overlay.runtime.test_command(worktree),
            extra_args=ctx.args,
            label="Tests",
            missing_message="No test command configured in the overlay.",
        )

    @command(context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
    def lint(
        self,
        ctx: typer.Context,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Run the overlay's lint pipeline on this worktree.

        Extra arguments after ``--`` are appended to the lint command
        (e.g. ``t3 <overlay> run lint -- --files src/foo.py``).
        """
        worktree = resolve_worktree(path)
        overlay = get_overlay()
        return self._dispatch_task(
            worktree,
            overlay.runtime.lint_command(worktree),
            extra_args=ctx.args,
            label="Lint",
            missing_message="No lint command configured in the overlay.",
        )

    def _dispatch_task(
        self,
        worktree: "Worktree",
        cmd: list[str] | RunCommand,
        *,
        extra_args: list[str],
        label: str,
        missing_message: str,
    ) -> str:
        """Stream a worktree task command, surfacing a non-zero exit as ``SystemExit(1)``.

        Shared by ``run tests`` and ``run lint``: an overlay that cannot run
        the task explicitly asked for must stop the caller (CI/loop), not
        exit 0 (#932). The ``label`` names the task in the success/failure
        message; ``missing_message`` is shown when the overlay declares no
        command.
        """
        if not cmd:
            self.stderr.write(missing_message)
            raise SystemExit(1)

        if isinstance(cmd, RunCommand):
            args = list(cmd.args)
            cwd: Path | str | None = cmd.cwd
            cmd_env = cmd.env
        else:
            args = list(cmd)
            cwd = None
            cmd_env: dict[str, str] = {}

        args.extend(extra_args)
        env: dict[str, str] = {**os.environ, **get_overlay().provisioning.env_extra(worktree), **cmd_env}
        env.pop("VIRTUAL_ENV", None)

        rc = run_streamed(args, cwd=cwd, env=env, check=False)
        if rc != 0:
            self.stderr.write(f"{label} failed (exit {rc}).")
            raise SystemExit(1)
        return f"{label} completed."

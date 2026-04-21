import os
from pathlib import Path
from typing import cast

import typer
from django.core.management.base import OutputWrapper
from django_typer.management import TyperCommand, command

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay
from teatree.core.resolve import resolve_worktree
from teatree.core.step_runner import ProvisionReport, run_provision_steps, run_step
from teatree.core.worktree_env import CACHE_FILENAME, write_env_cache
from teatree.timeouts import TimeoutConfig, load_timeouts
from teatree.utils import redis_container
from teatree.utils.ports import find_free_ports, get_worktree_ports
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail


def _append_envrc_lines(wt_path: str, lines: list[str]) -> None:
    """Append missing lines to the worktree .envrc (idempotent)."""
    envrc = Path(wt_path) / ".envrc"
    existing = envrc.read_text() if envrc.is_file() else ""
    missing = [ln for ln in lines if ln not in existing]
    if missing:
        envrc.write_text(existing.rstrip() + "\n" + "\n".join(missing) + "\n")


def _write_skill_metadata_cache() -> None:
    """Write overlay skill metadata to XDG cache for hook consumption."""
    from teatree.core.views._startup import _write_skill_metadata_cache as _write  # noqa: PLC0415

    _write()


def _setup_worktree_dir(wt_path: str, worktree: Worktree, overlay: OverlayBase, stdout: OutputWrapper) -> None:
    """Configure direnv and pre-commit for the worktree directory."""
    if not wt_path or not Path(wt_path).is_dir():
        return
    core_lines = [f"dotenv {CACHE_FILENAME}"]
    _append_envrc_lines(wt_path, core_lines + overlay.get_envrc_lines(worktree))
    result = run_step("direnv-allow", ["direnv", "allow", wt_path], check=False)
    if not result.success:
        stdout.write(f"  direnv allow: {result.error}")
    if (Path(wt_path) / ".pre-commit-config.yaml").is_file():
        stdout.write("  Running: prek install")
        result = run_step("prek-install", ["prek", "install", "-f"], cwd=wt_path, check=False)
        if not result.success:
            stdout.write(f"  prek install: {result.error}")


def _register_new_repos(worktree: Worktree, stdout: OutputWrapper) -> None:
    """Discover git worktrees in the ticket directory that aren't in the DB yet."""
    ticket_or_none = worktree.ticket
    if ticket_or_none is None:
        return
    ticket_dir = (worktree.extra or {}).get("worktree_path", "")
    if not ticket_dir:
        return
    ticket_path = Path(ticket_dir).parent
    if not ticket_path.is_dir():
        return

    ticket = Ticket.objects.get(pk=ticket_or_none.pk)
    known_paths = {(wt.extra or {}).get("worktree_path", "") for wt in ticket.worktrees.all()}

    for entry in sorted(ticket_path.iterdir()):
        if not entry.is_dir():
            continue
        git_marker = entry / ".git"
        if not git_marker.exists():
            continue
        if git_marker.is_dir():
            continue
        entry_str = str(entry)
        if entry_str in known_paths:
            continue
        Worktree.objects.create(
            ticket=ticket,
            repo_path=entry.name,
            branch=worktree.branch,
            extra={"worktree_path": entry_str},
        )
        stdout.write(f"  Discovered new repo: {entry.name}")


def _resolve_typer_defaults(
    variant: "str | object", overlay: "str | object", verbose: "bool | object"
) -> tuple[str, str, bool]:
    return (
        variant if isinstance(variant, str) else "",
        overlay if isinstance(overlay, str) else "",
        verbose if isinstance(verbose, bool) else False,
    )


def _update_ticket_variant(ticket: "Ticket", variant: str) -> None:
    """Update ticket variant and recompute db_name for all worktrees."""
    if not variant or ticket.variant == variant:
        return
    ticket.variant = variant
    ticket.save(update_fields=["variant"])
    for wt in ticket.worktrees.all():  # type: ignore[attr-defined]
        old_db = wt.db_name
        wt.db_name = wt._build_db_name()  # noqa: SLF001
        if wt.db_name != old_db:
            wt.save(update_fields=["db_name"])


def compose_project(worktree: Worktree) -> str:
    """Return the docker-compose project name for this worktree."""
    ticket = worktree.ticket
    return f"{worktree.repo_path}-wt{ticket.ticket_number}" if ticket else worktree.repo_path


def _compose_env(ports: dict[str, int]) -> dict[str, str]:
    """Build env vars for docker-compose port mapping.

    Sets both ``*_HOST_PORT`` (compose port-mapping) and commonly used
    aliases (``POSTGRES_PORT``, ``CORS_WHITE_FRONT``) so that runtime
    consumers (DSLR, manage.py, overlays) always see the allocated ports.
    """
    frontend = ports.get("frontend", 4200)
    return {
        "BACKEND_HOST_PORT": str(ports.get("backend", 8000)),
        "FRONTEND_HOST_PORT": str(frontend),
        "POSTGRES_HOST_PORT": str(ports.get("postgres", 5432)),
        "POSTGRES_PORT": str(ports.get("postgres", 5432)),
        "CORS_WHITE_FRONT": f"http://localhost:{frontend}",
    }


def _docker_compose_down(project: str, stdout: OutputWrapper, *, timeout: int | None = 30) -> None:
    """Stop and remove containers for the compose project."""
    try:
        result = run_allowed_to_fail(
            ["docker", "compose", "-p", project, "down", "--remove-orphans"],
            expected_codes=None,
            timeout=timeout,
        )
        if result.returncode != 0:
            stdout.write(f"  docker compose down: {result.stderr.strip()[:300]}")
    except TimeoutExpired:
        stdout.write(f"  docker compose down: timed out after {timeout}s")


def _compose_files(compose_file: str) -> list[str]:
    """Return -f flags for compose file and its override (if present)."""
    flags = ["-f", compose_file]
    override = Path(compose_file).parent / "docker-compose.override.yml"
    if override.is_file():
        flags.extend(["-f", str(override)])
    return flags


class Command(TyperCommand):
    _verbose: bool = True
    _timeouts: TimeoutConfig = TimeoutConfig()

    def _init_timeouts(self, overlay: OverlayBase | None = None, *, no_timeout: bool = False) -> None:
        if no_timeout:
            self._timeouts = TimeoutConfig(values=dict.fromkeys(self._timeouts.values, 0))
        else:
            self._timeouts = load_timeouts(overlay)

    def _docker_compose_up(
        self,
        project: str,
        compose_file: str,
        env: dict[str, str],
        *,
        timeout: int | None = 60,
    ) -> bool:
        """Start all services via docker-compose."""
        cmd = [
            "docker",
            "compose",
            "-p",
            project,
            *_compose_files(compose_file),
            "up",
            "-d",
            "--no-build",
            "--pull=never",
        ]
        try:
            result = run_allowed_to_fail(cmd, env=env, expected_codes=None, timeout=timeout)
        except TimeoutExpired:
            self.stderr.write(f"  docker compose up: timed out after {timeout}s")
            return False
        if result.returncode != 0:
            self.stderr.write(f"  docker compose up failed (exit {result.returncode}):")
            self.stderr.write(f"  stderr: {result.stderr.strip()}")
            self.stderr.write(f"  stdout: {result.stdout.strip()[:500]}")
            return False
        self.stdout.write("  docker compose up -d: OK")
        return True

    @command()
    def setup(  # noqa: PLR0913, PLR0917
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        variant: str = typer.Option("", help="Tenant variant. Updates ticket if provided."),
        overlay: str = typer.Option("", help="Overlay name (auto-detects if empty)."),
        slow_import: bool = typer.Option(  # noqa: FBT001
            default=False, help="Allow slow DB fallbacks (pg_restore, remote dump). DSLR-only by default."
        ),
        verbose: bool = typer.Option(default=True, help="Show step stdout/stderr."),  # noqa: FBT001
        no_timeout: bool = typer.Option(default=False, help="Disable operation timeouts."),  # noqa: FBT001
    ) -> int:
        """Provision a worktree (DB name, env file, overlay steps). No port allocation.

        Idempotent — safe to re-run. Auto-retries DB import when the DB
        doesn't exist, regardless of previous failure count.
        """
        variant, overlay, verbose = _resolve_typer_defaults(variant, overlay, verbose)
        self._verbose = verbose
        if overlay:
            os.environ["T3_OVERLAY_NAME"] = overlay
        worktree = resolve_worktree(path)
        ticket = Ticket.objects.get(pk=worktree.ticket.pk)

        _update_ticket_variant(ticket, variant)
        _register_new_repos(worktree, self.stdout)

        redis_container.ensure_running()
        Ticket.objects.allocate_redis_slot(ticket)

        resolved_overlay = get_overlay()
        self._init_timeouts(resolved_overlay, no_timeout=no_timeout)

        failed_repos: list[str] = []
        for wt in ticket.worktrees.all():
            try:
                report = self._provision_worktree(wt, resolved_overlay, slow_import=slow_import)
                if not report.success:
                    failed_repos.append(wt.repo_path)
            except Exception as exc:  # noqa: BLE001
                failed_repos.append(wt.repo_path)
                self.stderr.write(f"  ERROR provisioning {wt.repo_path}: {exc}")

        if failed_repos:
            self.stderr.write(f"  {len(failed_repos)} worktree(s) failed: {', '.join(failed_repos)}")

        _write_skill_metadata_cache()
        return int(worktree.pk)

    def _provision_worktree(
        self, worktree: Worktree, overlay: "OverlayBase", *, slow_import: bool = False
    ) -> ProvisionReport:
        self.stdout.write(f"  Provisioning: {worktree.repo_path}")

        if worktree.state == Worktree.State.CREATED:
            worktree.provision()
            worktree.save()

        spec = write_env_cache(worktree)
        if spec:
            self.stdout.write(f"  Written: {spec.path}")

        _setup_worktree_dir((worktree.extra or {}).get("worktree_path", ""), worktree, overlay, self.stdout)

        if overlay.get_db_import_strategy(worktree) is not None:
            self._run_db_import(worktree, overlay, slow_import=slow_import)

        provision_report = run_provision_steps(
            overlay.get_provision_steps(worktree),
            verbose=self._verbose,
            stdout_writer=self.stdout.write,
            stderr_writer=self.stderr.write,
        )

        post_db_report = self._run_post_db_steps(overlay, worktree)

        pre_run_steps = []
        for service_name in overlay.get_run_commands(worktree):
            pre_run_steps.extend(overlay.get_pre_run_steps(worktree, service_name))
        pre_run_report = run_provision_steps(
            pre_run_steps,
            verbose=self._verbose,
            stdout_writer=self.stdout.write,
            stderr_writer=self.stderr.write,
            stop_on_required_failure=False,
        )

        self._run_health_checks(worktree, overlay)

        combined = ProvisionReport(
            steps=provision_report.steps + post_db_report.steps + pre_run_report.steps,
        )
        self._print_diagnostics(worktree, combined)
        return combined

    def _run_db_import(self, worktree: Worktree, overlay: OverlayBase, *, slow_import: bool = False) -> None:
        from teatree.utils.db import db_exists  # noqa: PLC0415

        if worktree.db_name:
            try:
                if db_exists(worktree.db_name):
                    self.stdout.write(f"  DB exists: {worktree.db_name} — skipping import")
                    return
            except FileNotFoundError:
                pass  # psql not available — proceed with import attempt

        self.stdout.write("  Running: db-import")
        env = {**os.environ, **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        os.environ.update(env)
        if overlay.db_import(worktree, slow_import=slow_import):
            extra = worktree.extra or {}
            extra.pop("db_import_failures", None)
            worktree.extra = extra
            worktree.save(update_fields=["extra"])
        else:
            self.stderr.write("  WARNING: DB import failed. Continuing with provision steps...")

    def _run_post_db_steps(self, overlay: OverlayBase, worktree: Worktree) -> ProvisionReport:
        steps = list(overlay.get_post_db_steps(worktree))
        reset_step = overlay.get_reset_passwords_command(worktree)
        if reset_step:
            steps.append(reset_step)
        return run_provision_steps(
            steps,
            verbose=self._verbose,
            stdout_writer=self.stdout.write,
            stderr_writer=self.stderr.write,
            stop_on_required_failure=False,
        )

    def _run_health_checks(self, worktree: Worktree, overlay: OverlayBase) -> None:
        checks = overlay.get_health_checks(worktree)
        if not checks:
            return
        failures: list[str] = []
        for check in checks:
            try:
                if not check.check():
                    failures.append(check.name)
                    self.stderr.write(f"  HEALTH CHECK FAILED: {check.name} — {check.description}")
                elif self._verbose:
                    self.stdout.write(f"  HEALTH CHECK OK: {check.name}")
            except Exception as exc:  # noqa: BLE001
                failures.append(check.name)
                self.stderr.write(f"  HEALTH CHECK ERROR: {check.name} — {exc}")
        if failures:
            self.stderr.write(f"  {len(failures)}/{len(checks)} health check(s) failed.")

    def _print_diagnostics(self, worktree: Worktree, report: ProvisionReport) -> None:
        """Print a structured checklist summarizing worktree state after provisioning."""
        wt_path = (worktree.extra or {}).get("worktree_path", "")
        self.stdout.write(f"\n  ── {worktree.repo_path} ──")
        cache_path = Path(wt_path).parent / ".t3-cache" / CACHE_FILENAME if wt_path else None
        checks = [
            ("worktree dir", bool(wt_path and Path(wt_path).is_dir())),
            (CACHE_FILENAME, bool(cache_path and cache_path.is_file())),
            ("DB name", bool(worktree.db_name)),
        ]
        checks.extend((step.name, step.success) for step in report.steps)
        ok = sum(1 for _, passed in checks if passed)
        for name, passed in checks:
            status = "OK" if passed else "FAIL"
            self.stdout.write(f"  [{status}] {name}")
        self.stdout.write(f"  {ok}/{len(checks)} checks passed\n")

    @command()
    def diagnose(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> dict[str, object]:
        """Check worktree health: git dir, env file, DB, docker services."""
        worktree = resolve_worktree(path)
        wt_path = (worktree.extra or {}).get("worktree_path", "")
        ticket_dir = Path(wt_path).parent if wt_path else None

        cache_file = ticket_dir / ".t3-cache" / CACHE_FILENAME if ticket_dir else None
        checks: dict[str, object] = {
            "state": worktree.state,
            "repo_path": worktree.repo_path,
            "worktree_dir": bool(wt_path and Path(wt_path).is_dir()),
            "git_marker": bool(wt_path and (Path(wt_path) / ".git").exists()),
            "env_cache": bool(cache_file and cache_file.is_file()),
            "db_name": worktree.db_name,
        }

        # Docker compose status
        project = compose_project(worktree)
        result = run_allowed_to_fail(
            ["docker", "compose", "-p", project, "ps", "--format", "{{.Name}} {{.State}}"],
            expected_codes=None,
        )
        checks["docker_services"] = result.stdout.strip() if result.returncode == 0 else "not running"

        # Print human-readable checklist
        self.stdout.write(f"\n  ── {worktree.repo_path} ({worktree.state}) ──")
        for key in ("worktree_dir", "git_marker", "env_cache"):
            status = "OK" if checks[key] else "FAIL"
            self.stdout.write(f"  [{status}] {key}")
        self.stdout.write(f"  [{'OK' if checks['db_name'] else 'FAIL'}] DB name: {checks['db_name'] or '(none)'}")
        self.stdout.write(f"  docker: {checks['docker_services']}")

        return checks

    @command()
    def start(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        variant: str = typer.Option("", help="Tenant variant (passed to setup if needed)."),
        overlay: str = typer.Option("", help="Overlay name (auto-detects if empty)."),
        verbose: bool = typer.Option(default=True, help="Show step stdout/stderr."),  # noqa: FBT001
        no_timeout: bool = typer.Option(default=False, help="Disable operation timeouts."),  # noqa: FBT001
    ) -> str:
        """Provision (if needed) and start all services for the ticket.

        Runs setup for all worktrees in the ticket, then starts docker-compose
        services for each. Allocates free host ports at runtime.
        Safe to re-run — stops previous containers first.
        """
        # 0. Always run setup first (idempotent — provisions all ticket worktrees)
        self.setup(path=path, variant=variant, overlay=overlay, verbose=verbose, no_timeout=no_timeout)

        _, overlay_str, verbose_bool = _resolve_typer_defaults(variant, overlay, verbose)
        self._verbose = verbose_bool
        if overlay_str:
            os.environ["T3_OVERLAY_NAME"] = overlay_str
        worktree = resolve_worktree(path)
        resolved_overlay = get_overlay()
        self._init_timeouts(resolved_overlay, no_timeout=no_timeout)

        # Allocate one set of ports shared across the ticket
        from teatree.config import load_config  # noqa: PLC0415

        workspace_dir = str(load_config().user.workspace_dir)
        ports = find_free_ports(workspace_dir)
        self.stdout.write(f"  Ports: {ports}")

        # Start services for every worktree in the ticket
        ticket = Ticket.objects.get(pk=worktree.ticket.pk)
        failed_repos: list[str] = []
        for wt in ticket.worktrees.all():
            try:
                self._start_worktree(wt, resolved_overlay, ports)
            except Exception as exc:  # noqa: BLE001
                failed_repos.append(wt.repo_path)
                self.stderr.write(f"  ERROR starting {wt.repo_path}: {exc}")

        if failed_repos:
            self.stderr.write(f"  {len(failed_repos)} worktree(s) failed: {', '.join(failed_repos)}")

        self.stdout.write(f"  Ports: {ports}")
        if failed_repos:
            return "error"
        return worktree.state

    def _start_worktree(self, worktree: Worktree, overlay: "OverlayBase", ports: dict[str, int]) -> None:
        """Start docker-compose services for a single worktree."""
        project = compose_project(worktree)
        self.stdout.write(f"\n  ── Starting {worktree.repo_path} ──")

        # Stop previous containers
        _docker_compose_down(project, self.stdout, timeout=self._timeouts.get("docker_compose_down"))

        # Inject allocated ports into process env so overlay steps can read them
        port_env = _compose_env(ports)
        for key, value in port_env.items():
            os.environ[key] = value

        # Run pre-run steps (need port env for patch-customer-json etc.)
        commands = overlay.get_run_commands(worktree)
        pre_run_steps = []
        for service_name in commands:
            pre_run_steps.extend(overlay.get_pre_run_steps(worktree, service_name))
        run_provision_steps(
            pre_run_steps,
            verbose=self._verbose,
            stdout_writer=self.stdout.write,
            stderr_writer=self.stderr.write,
            stop_on_required_failure=False,
        )

        # Regenerate cache (includes overlay extras which now see correct ports).
        # Unconditional on every start — the plan calls for no stale cache.
        write_env_cache(worktree)

        # Start services via docker-compose
        compose_file = overlay.get_compose_file(worktree)
        if not compose_file:
            self.stdout.write(f"    No docker-compose file for {worktree.repo_path} — skipping.")
            return

        env = {**os.environ, **_compose_env(ports), **overlay.get_env_extra(worktree)}
        env.pop("VIRTUAL_ENV", None)
        ok = self._docker_compose_up(
            project,
            compose_file,
            env,
            timeout=self._timeouts.get("docker_compose_up"),
        )

        if not ok:
            msg = f"docker compose up failed for {worktree.repo_path}"
            raise RuntimeError(msg)

        # FSM transition
        worktree.start_services(services=list(commands))
        worktree.save()
        self.stdout.write("  docker compose up -d: OK")

    @command()
    def status(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> dict[str, object]:
        worktree = resolve_worktree(path)
        project = compose_project(worktree)
        ports = get_worktree_ports(project)
        return {
            "state": worktree.state,
            "repo_path": worktree.repo_path,
            "branch": worktree.branch,
            "ports": ports,
        }

    @command()
    def teardown(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        worktree = resolve_worktree(path)
        project = compose_project(worktree)
        _docker_compose_down(project, self.stdout)
        worktree.teardown()
        worktree.save()
        return worktree.state

    @command()
    def clean(self, path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty).")) -> str:
        """Teardown worktree — stop containers, drop DB, clean state."""
        worktree = resolve_worktree(path)
        project = compose_project(worktree)
        _docker_compose_down(project, self.stdout)

        if worktree.db_name:
            from teatree.utils.db import pg_env, pg_host, pg_user  # noqa: PLC0415

            run_allowed_to_fail(
                ["dropdb", "-h", pg_host(), "-U", pg_user(), "--if-exists", worktree.db_name],
                env=pg_env(),
                expected_codes=None,
            )

        worktree.teardown()
        worktree.save()
        return f"Cleaned worktree {worktree.repo_path} ({worktree.state})"

    @command(name="smoke-test")
    def smoke_test(self) -> dict[str, object]:
        """Quick health check: overlay loads, CLI responds, imports OK."""
        checks: dict[str, object] = {}

        try:
            overlay = get_overlay()
            checks["overlay"] = {"status": "ok", "repos": overlay.get_repos()}
        except Exception as exc:  # noqa: BLE001
            checks["overlay"] = {"status": "error", "detail": str(exc)}

        try:
            result = run_allowed_to_fail(
                ["uv", "run", "t3", "--help"],
                expected_codes=None,
                timeout=30,
            )
            checks["cli"] = {"status": "ok" if result.returncode == 0 else "error"}
        except TimeoutExpired:
            checks["cli"] = {"status": "error", "detail": "t3 --help timed out"}

        try:
            count = Worktree.objects.count()
            checks["database"] = {"status": "ok", "worktrees": count}
        except Exception as exc:  # noqa: BLE001
            checks["database"] = {"status": "error", "detail": str(exc)}

        hook_config = Path("." if Path(".pre-commit-config.yaml").is_file() else os.environ.get("PWD", "."))
        hook_file = hook_config / ".pre-commit-config.yaml"
        if hook_file.is_file():
            try:
                from importlib import import_module  # noqa: PLC0415

                yaml = import_module("yaml")
                yaml.safe_load(hook_file.read_text(encoding="utf-8"))
                checks["hooks"] = {"status": "ok"}
            except Exception as exc:  # noqa: BLE001
                checks["hooks"] = {"status": "error", "detail": str(exc)}
        else:
            checks["hooks"] = {"status": "skipped", "detail": "no .pre-commit-config.yaml"}

        # Validate core Python imports
        import_errors: list[str] = []
        for module in ("teatree.core.overlay", "teatree.core.models", "teatree.utils.git", "teatree.utils.ports"):
            try:
                __import__(module)
            except ImportError as exc:
                import_errors.append(f"{module}: {exc}")
        checks["imports"] = {"status": "ok" if not import_errors else "error", "errors": import_errors}

        # Print human-readable summary
        for name, check_val in checks.items():
            detail = cast("dict[str, object]", check_val) if isinstance(check_val, dict) else {}
            status = str(detail.get("status", "unknown"))
            self.stdout.write(f"  [{status.upper()}] {name}")

        return checks

    @command(name="visit-phase")
    def visit_phase(self, ticket_id: int, phase: str) -> str:
        """Mark a phase as visited on the ticket's latest session."""
        from teatree.core.models import Session  # noqa: PLC0415

        ticket = Ticket.objects.get(pk=ticket_id)
        session = ticket.sessions.order_by("-pk").first()
        if session is None:
            session = Session.objects.create(ticket=ticket)
        session.visit_phase(phase)
        return f"Phase '{phase}' marked as visited on session {session.pk}"

    @command()
    def diagram(self, model: str = "worktree", ticket: int | None = None) -> str:
        """Print a state diagram as Mermaid. Models: worktree, ticket, task."""
        if ticket is not None:
            from teatree.core.selectors import build_ticket_lifecycle_mermaid  # noqa: PLC0415

            return build_ticket_lifecycle_mermaid(ticket)

        model_map: dict[str, type] = {"worktree": Worktree, "ticket": Ticket}
        if model == "task":
            return _task_diagram()
        if model not in model_map:
            return f"Unknown model: {model}. Choose from: worktree, ticket, task"
        return _fsm_diagram(model_map[model])


def _fsm_diagram(model: type) -> str:
    """Generate a Mermaid state diagram from django-fsm transitions."""
    field = model._meta.get_field("state")  # type: ignore[attr-defined]  # noqa: SLF001
    default = field.default
    lines = ["stateDiagram-v2", f"    [*] --> {default}"]

    for t in field.get_all_transitions(model):
        source = t.source
        target = t.target
        if source == "*":
            for choice_val, _label in field.choices:
                lines.append(f"    {choice_val} --> {target}: {t.name}()")
        else:
            lines.append(f"    {source} --> {target}: {t.name}()")

    return "\n".join(lines)


def _task_diagram() -> str:
    """Task uses manual status management, not FSM transitions."""
    lines = [
        "stateDiagram-v2",
        "    [*] --> pending",
        "    pending --> claimed: claim()",
        "    claimed --> completed: complete()",
        "    claimed --> failed: fail()",
        "    completed --> [*]",
        "    failed --> [*]",
    ]
    return "\n".join(lines)

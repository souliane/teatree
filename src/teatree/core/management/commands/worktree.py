"""Single-worktree CLI surface.

Each subcommand is a thin wrapper around a ``Worktree`` FSM transition. The
transition fires its on_commit hook to enqueue the corresponding ``@task``
worker (BLUEPRINT §4); the CLI also runs the runner synchronously so the
operator gets immediate stdout while the queued worker remains available
for headless retries.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import typer
from django.db import transaction
from django_typer.management import TyperCommand, command

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay
from teatree.core.readiness import run_probes
from teatree.core.resolve import resolve_worktree
from teatree.core.runners import (
    WorktreeProvisionRunner,
    WorktreeStartRunner,
    WorktreeTeardownRunner,
    WorktreeVerifyRunner,
)
from teatree.core.runners.worktree_start import compose_project
from teatree.core.worktree_env import CACHE_FILENAME
from teatree.docker.build import ensure_base_image
from teatree.utils import redis_container
from teatree.utils.ports import find_free_ports, get_worktree_ports
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail


class WorktreeStatus(TypedDict):
    state: str
    repo_path: str
    branch: str
    ports: dict[str, int]


class WorktreeDiagnose(TypedDict):
    state: str
    repo_path: str
    worktree_dir: bool
    git_marker: bool
    env_cache: bool
    db_name: str
    docker_services: str


class SmokeCheck(TypedDict, total=False):
    status: str
    detail: str
    repos: list[str]
    worktrees: int
    errors: list[str]


class SmokeReport(TypedDict, total=False):
    overlay: SmokeCheck
    cli: SmokeCheck
    database: SmokeCheck
    hooks: SmokeCheck
    imports: SmokeCheck


def validate_docker_service_contract(overlay: OverlayBase, worktree: Worktree) -> None:
    """Raise when the overlay declares a docker service it didn't configure.

    Catches contract drift at provision time instead of at ``start`` when
    compose silently ignores the unknown service.
    """
    declared = set(overlay.get_services_config(worktree))
    docker = set(overlay.get_docker_services(worktree))
    missing = docker - declared
    if missing:
        msg = (
            f"Overlay {overlay.__class__.__name__} declares docker services "
            f"{sorted(missing)} not present in get_services_config "
            f"(declared: {sorted(declared)}). "
            "Either add them to get_services_config or remove from get_docker_services."
        )
        raise RuntimeError(msg)


def _build_base_images(overlay: OverlayBase, worktree: Worktree, *, writer: Callable[[str], object]) -> None:
    seen: set[str] = set()
    for cfg in overlay.get_base_images(worktree):
        key = f"{cfg.image_name}|{cfg.build_context}"
        if key in seen:
            continue
        seen.add(key)
        writer(f"  Base image: {cfg.image_name} (context: {cfg.build_context})")
        tag = ensure_base_image(cfg)
        writer(f"  [OK] {tag}")


def _update_ticket_variant(ticket: Ticket, variant: str) -> None:
    if not variant or ticket.variant == variant:
        return
    ticket.variant = variant
    ticket.save(update_fields=["variant"])
    for wt in ticket.worktrees.all():  # type: ignore[attr-defined]
        old_db = wt.db_name
        wt.db_name = wt._build_db_name()  # noqa: SLF001
        if wt.db_name != old_db:
            wt.save(update_fields=["db_name"])


def _resolve_typer_defaults(variant: "str | object", overlay: "str | object") -> tuple[str, str]:
    return (
        variant if isinstance(variant, str) else "",
        overlay if isinstance(overlay, str) else "",
    )


class Command(TyperCommand):
    @command()
    def provision(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        variant: str = typer.Option("", help="Tenant variant. Updates ticket if provided."),
        overlay: str = typer.Option("", help="Overlay name (auto-detects if empty)."),
        slow_import: bool = typer.Option(  # noqa: FBT001
            default=False, help="Allow slow DB fallbacks (pg_restore, remote dump). DSLR-only by default."
        ),
    ) -> int:
        """Run DB import + env cache + direnv + prek + overlay setup steps for one worktree.

        Thin wrapper around ``Worktree.provision()``: the FSM flips
        CREATED → PROVISIONED and enqueues ``execute_worktree_provision``;
        the runner also runs synchronously here so the operator sees
        immediate output. Idempotent — re-running is safe.
        """
        variant, overlay_name = _resolve_typer_defaults(variant, overlay)
        if overlay_name:
            os.environ["T3_OVERLAY_NAME"] = overlay_name
        worktree = resolve_worktree(path)
        ticket = Ticket.objects.get(pk=worktree.ticket.pk)

        _update_ticket_variant(ticket, variant)
        resolved_overlay = get_overlay()
        if resolved_overlay.uses_redis():
            redis_container.ensure_running()
            Ticket.objects.allocate_redis_slot(ticket)
        validate_docker_service_contract(resolved_overlay, worktree)
        _build_base_images(resolved_overlay, worktree, writer=self.stdout.write)

        with transaction.atomic():
            if worktree.state in {Worktree.State.CREATED, Worktree.State.PROVISIONED}:
                worktree.provision()
                worktree.save()

        result = WorktreeProvisionRunner(worktree, overlay=resolved_overlay, slow_import=slow_import).run()
        worktree.refresh_from_db()
        self.stdout.write(f"  {result.detail}")
        if not result.ok:
            self.stderr.write(f"  Provision failed for {worktree.repo_path}")
            raise SystemExit(1)
        return int(worktree.pk)

    def _check_readiness_probes(self, worktree: Worktree, overlay: OverlayBase) -> None:
        """Run overlay readiness probes; raise SystemExit(1) on any failure.

        Shared by ``start``, ``verify``, ``ready`` so all three honor the same
        runtime health gate. ``start`` and ``verify`` cannot return success
        when the started/healthy services are silently broken (raw
        translations, missing CORS headers, fixture-seed integrity).
        """
        probes = overlay.get_readiness_probes(worktree)
        if not probes:
            self.stdout.write("  No readiness probes defined for this overlay.")
            return
        results = run_probes(probes)
        for r in results:
            self.stdout.write(f"  {r.format()}")
        failures = [r for r in results if not r.passed]
        if failures:
            self.stderr.write(f"  {len(failures)} of {len(results)} probe(s) failed")
            raise SystemExit(1)

    @command()
    def start(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Boot ``docker compose up`` for one worktree.

        Thin wrapper around ``Worktree.start_services()``: the FSM advances
        to SERVICES_UP and enqueues ``execute_worktree_start``; the runner
        also runs synchronously here so the operator sees immediate output.
        Allocates free host ports, refreshes the env cache, runs overlay
        pre-run steps, then ``docker compose up -d``. After the runner
        succeeds, runs the overlay's readiness probes — exits 1 if any fail.
        """
        worktree = resolve_worktree(path)
        resolved_overlay = get_overlay()

        from teatree.config import load_config  # noqa: PLC0415

        ports = find_free_ports(
            str(load_config().user.workspace_dir),
            resolved_overlay.get_required_ports(worktree),
        )
        self.stdout.write(f"  Ports: {ports}")

        commands = list(resolved_overlay.get_run_commands(worktree))
        with transaction.atomic():
            worktree.start_services(services=commands)
            worktree.save()

        result = WorktreeStartRunner(worktree, overlay=resolved_overlay, ports=ports).run()
        worktree.refresh_from_db()
        self.stdout.write(f"  {result.detail}")
        if not result.ok:
            self.stderr.write(f"  Start failed for {worktree.repo_path}")
            raise SystemExit(1)
        self._check_readiness_probes(worktree, resolved_overlay)
        return worktree.state

    @command()
    def verify(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Run overlay health checks for one worktree.

        Thin wrapper around ``Worktree.verify()``: SERVICES_UP → READY +
        runner records URLs and reports failed checks. After the runner,
        runs the overlay's readiness probes — exits 1 if any fail.
        """
        worktree = resolve_worktree(path)
        resolved_overlay = get_overlay()
        with transaction.atomic():
            worktree.verify()
            worktree.save()
        result = WorktreeVerifyRunner(worktree, overlay=resolved_overlay).run()
        self.stdout.write(f"  {result.detail}")
        worktree.refresh_from_db()
        self._check_readiness_probes(worktree, resolved_overlay)
        return worktree.state

    @command()
    def ready(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Run runtime readiness probes for one worktree.

        Strict: exits 0 iff every probe declared by ``OverlayBase.get_readiness_probes``
        passes. Does not mutate worktree state. Use after ``start`` to verify
        the env is actually serving — answers the question ``verify`` cannot
        (HTTP, CORS round-trip, end-to-end auth, fixture seed integrity).
        """
        worktree = resolve_worktree(path)
        resolved_overlay = get_overlay()
        self._check_readiness_probes(worktree, resolved_overlay)
        return "ok"

    @command()
    def teardown(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> str:
        """Stop docker, drop DB, remove git worktree, delete row.

        Thin wrapper around ``Worktree.teardown()``: the FSM resets to
        CREATED and enqueues ``execute_worktree_teardown``; the runner
        also runs synchronously here so the operator sees immediate
        output. Folds the previous ``teardown`` + ``clean`` commands
        into a single canonical path.
        """
        worktree = resolve_worktree(path)
        repo_path = worktree.repo_path
        # Snapshot before the transition body resets db_name/extra
        snapshot_db_name = worktree.db_name
        snapshot_extra = worktree.get_extra()
        with transaction.atomic():
            worktree.teardown()
            worktree.save()
        result = WorktreeTeardownRunner(
            worktree,
            snapshot_db_name=snapshot_db_name,
            snapshot_extra=snapshot_extra,
        ).run()
        self.stdout.write(f"  {result.detail}")
        if not result.ok:
            self.stderr.write(f"  Teardown failed for {repo_path}")
            raise SystemExit(1)
        return result.detail

    @command()
    def status(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> WorktreeStatus:
        """Report FSM state, branch, and allocated host ports for one worktree."""
        worktree = resolve_worktree(path)
        ports = get_worktree_ports(compose_project(worktree))
        return {
            "state": worktree.state,
            "repo_path": worktree.repo_path,
            "branch": worktree.branch,
            "ports": ports,
        }

    @command()
    def diagnose(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
    ) -> WorktreeDiagnose:
        """Print a structured health checklist for one worktree."""
        worktree = resolve_worktree(path)
        wt_path = (worktree.extra or {}).get("worktree_path", "")
        ticket_dir = Path(wt_path).parent if wt_path else None
        cache_file = ticket_dir / ".t3-cache" / CACHE_FILENAME if ticket_dir else None

        project = compose_project(worktree)
        ps = run_allowed_to_fail(
            ["docker", "compose", "-p", project, "ps", "--format", "{{.Name}} {{.State}}"],
            expected_codes=None,
        )
        checks: WorktreeDiagnose = {
            "state": worktree.state,
            "repo_path": worktree.repo_path,
            "worktree_dir": bool(wt_path and Path(wt_path).is_dir()),
            "git_marker": bool(wt_path and (Path(wt_path) / ".git").exists()),
            "env_cache": bool(cache_file and cache_file.is_file()),
            "db_name": worktree.db_name,
            "docker_services": ps.stdout.strip() if ps.returncode == 0 else "not running",
        }

        self.stdout.write(f"\n  ── {worktree.repo_path} ({worktree.state}) ──")
        for key in ("worktree_dir", "git_marker", "env_cache"):
            ok = "OK" if checks[key] else "FAIL"
            self.stdout.write(f"  [{ok}] {key}")
        self.stdout.write(f"  [{'OK' if checks['db_name'] else 'FAIL'}] DB name: {checks['db_name'] or '(none)'}")
        self.stdout.write(f"  docker: {checks['docker_services']}")
        return checks

    @command(name="smoke-test")
    def smoke_test(self) -> SmokeReport:
        """Quick health check: overlay loads, CLI responds, imports OK."""
        checks: SmokeReport = {}

        try:
            overlay = get_overlay()
            checks["overlay"] = {"status": "ok", "repos": overlay.get_repos()}
        except Exception as exc:  # noqa: BLE001
            checks["overlay"] = {"status": "error", "detail": str(exc)}

        try:
            result = run_allowed_to_fail(["uv", "run", "t3", "--help"], expected_codes=None, timeout=30)
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

        import_errors: list[str] = []
        for module in ("teatree.core.overlay", "teatree.core.models", "teatree.utils.git", "teatree.utils.ports"):
            try:
                __import__(module)
            except ImportError as exc:
                import_errors.append(f"{module}: {exc}")
        checks["imports"] = {"status": "ok" if not import_errors else "error", "errors": import_errors}

        for name in ("overlay", "cli", "database", "hooks", "imports"):
            entry = checks.get(name) or {}
            self.stdout.write(f"  [{entry.get('status', 'unknown').upper()}] {name}")
        return checks

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

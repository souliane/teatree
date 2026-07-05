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
from typing import IO, Annotated, TypedDict, cast

import typer
from django.db import transaction
from django_typer.management import TyperCommand, command

from teatree.core.diagrams import render_fsm_mermaid
from teatree.core.gates.local_stack_gate import acquire_or_enqueue
from teatree.core.machine_output import emit
from teatree.core.management.commands._workspace_docker import reap_stale_local_stacks
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay
from teatree.core.provision_postconditions import PostConditionOutcome, evaluate_post_conditions
from teatree.core.readiness import run_and_report_probes
from teatree.core.resolve import _ticket_by_number, resolve_worktree
from teatree.core.runners import (
    WorktreeProvisionRunner,
    WorktreeStartRunner,
    WorktreeTeardownRunner,
    WorktreeVerifyRunner,
    heal_missing_provisioned_db,
)
from teatree.core.step_runner import ProvisionReport
from teatree.core.worktree_env import compose_project, env_cache_path
from teatree.docker.build import ensure_base_image
from teatree.utils.ports import get_worktree_ports
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail


class ProvisionSummary(TypedDict):
    """Rendered summary of the worktree's last ``Worktree.extra['provision_report']``."""

    total_duration: float
    steps: int
    success: bool
    slowest_step: str
    slowest_step_duration: float


class WorktreeStatus(TypedDict, total=False):
    state: str
    repo_path: str
    branch: str
    ports: dict[str, int]
    provision_report: ProvisionSummary
    post_conditions: list[PostConditionOutcome]
    provisioned_ok: bool


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


def _provision_summary(worktree: Worktree) -> "ProvisionSummary | None":
    """Render ``Worktree.extra['provision_report']`` for ``status`` (souliane/teatree#2949).

    ``None`` when the worktree has never been provisioned under the
    instrumented runner — an absent key, not an error.
    """
    data = (worktree.extra or {}).get("provision_report")
    if not data:
        return None
    report = ProvisionReport.from_dict(data)
    slowest = report.slowest_step
    return {
        "total_duration": report.total_duration,
        "steps": len(report.steps),
        "success": report.success,
        "slowest_step": slowest.name if slowest is not None else "",
        "slowest_step_duration": slowest.duration if slowest is not None else 0.0,
    }


def _render_status(result: "WorktreeStatus", stream: IO[str]) -> None:
    stream.write(f"state: {result.get('state', '')}\n")
    stream.write(f"repo_path: {result.get('repo_path', '')}\n")
    stream.write(f"branch: {result.get('branch', '')}\n")
    stream.write(f"ports: {result.get('ports', {})}\n")
    summary = result.get("provision_report")
    if summary is not None:
        stream.write(
            f"provision: total={summary['total_duration']:.1f}s steps={summary['steps']} "
            f"success={summary['success']} slowest={summary['slowest_step']}\n"
        )
    post_conditions = result.get("post_conditions")
    if post_conditions is not None:
        stream.write(f"provisioned_ok: {result.get('provisioned_ok', False)}\n")
        for pc in post_conditions:
            stream.write(f"  [{'OK' if pc['passed'] else 'FAIL'}] {pc['name']} — {pc['reason']}\n")


def _render_diagnose(checks: "WorktreeDiagnose", stream: IO[str]) -> None:
    stream.write(f"\n  ── {checks['repo_path']} ({checks['state']}) ──\n")
    for key in ("worktree_dir", "git_marker", "env_cache"):
        ok = "OK" if checks[key] else "FAIL"
        stream.write(f"  [{ok}] {key}\n")
    stream.write(f"  [{'OK' if checks['db_name'] else 'FAIL'}] DB name: {checks['db_name'] or '(none)'}\n")
    stream.write(f"  docker: {checks['docker_services']}\n")


class Command(TyperCommand):
    @command()
    def provision(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        variant: str = typer.Option("", help="Tenant variant. Updates ticket if provided."),
        overlay: str = typer.Option("", help="Overlay name (auto-detects if empty)."),
        ticket: str = typer.Option(
            "",
            help="Pin attribution to this ticket number (overrides auto-register for a manual worktree).",
        ),
        slow_import: bool = typer.Option(  # noqa: FBT001
            default=False,
            help="Allow slow DB fallbacks (pg_restore, remote dump). DSLR-only by default.",
        ),
    ) -> int:
        """Run DB import + env cache + direnv + prek + overlay setup steps for one worktree.

        Thin wrapper around ``Worktree.provision()``: the FSM flips
        CREATED → PROVISIONED and enqueues ``execute_worktree_provision``;
        the runner also runs synchronously here so the operator sees
        immediate output. Idempotent — re-running is safe.

        ``--ticket`` pins attribution: a manually-added worktree
        (``git worktree add``) has no DB row, so resolution would auto-register
        and could cross-attach to an unrelated workspace sibling. The flag
        binds it to the named ticket instead.
        """
        variant, overlay_name = _resolve_typer_defaults(variant, overlay)
        if overlay_name:
            os.environ["T3_OVERLAY_NAME"] = overlay_name
        ticket_hint = self._resolve_ticket_hint(ticket)
        worktree = resolve_worktree(path, ticket_hint=ticket_hint)
        ticket_obj = Ticket.objects.get(pk=worktree.ticket.pk)

        _update_ticket_variant(ticket_obj, variant)
        # _update_ticket_variant mutated the ticket + worktree rows through
        # separate instances; reload so this worktree's cached ticket FK and
        # db_name reflect the new variant before provision() and the env render
        # read them (otherwise WT_VARIANT renders blank and db_name loses its
        # variant suffix).
        worktree.refresh_from_db()
        resolved_overlay = get_overlay()
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

    def _resolve_ticket_hint(self, ticket: str) -> Ticket | None:
        """Resolve the ``--ticket`` number to a Ticket, or raise if it is unknown."""
        if not ticket:
            return None
        hint = _ticket_by_number(ticket)
        if hint is None:
            self.stderr.write(f"  No ticket found for --ticket {ticket}")
            raise SystemExit(1)
        return hint

    def _heal_missing_provisioned_db(self, worktree: Worktree, overlay: OverlayBase) -> None:
        """Heal a ``provisioned`` worktree whose DB was never created (#1038).

        Thin CLI wrapper over :func:`heal_missing_provisioned_db`: reports the
        re-provision to the operator and turns a heal failure into ``SystemExit(1)``.
        """
        try:
            if heal_missing_provisioned_db(worktree, overlay):
                self.stdout.write(
                    f"  DB '{worktree.db_name}' was missing for a provisioned worktree "
                    "(interrupted provision?) — re-provisioned before start."
                )
        except RuntimeError as exc:
            self.stderr.write(f"  {exc}")
            raise SystemExit(1) from exc

    def _check_readiness_probes(self, worktree: Worktree, overlay: OverlayBase) -> None:
        """Run overlay readiness probes; raise SystemExit(1) on any failure.

        Shared by ``start``, ``verify``, ``ready`` so all three honor the same
        runtime health gate. Returns success when the overlay has no probes
        (the empty list is itself a valid contract).
        """
        probes = overlay.get_readiness_probes(worktree)
        if not probes:
            self.stdout.write("  No readiness probes defined for this overlay.")
            return
        summary = run_and_report_probes(probes, write_line=self.stdout.write, indent="  ")
        if summary.failures:
            self.stderr.write(f"  {summary.failures} of {summary.total} probe(s) failed")
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
        Refreshes the env cache, runs overlay pre-run steps, then
        ``docker compose up -d``. Docker auto-maps host ports; the actual
        ports are then queried via ``docker compose port`` and stored on
        ``Worktree.extra["ports"]``. After the runner succeeds, runs the
        overlay's readiness probes — exits 1 if any fail.
        """
        worktree = resolve_worktree(path)
        resolved_overlay = get_overlay()
        # #1038: a provision interrupted after the FSM flipped to PROVISIONED but
        # before the DB import ran leaves a worktree that looks ready yet whose
        # Postgres DB was never created — the start probe then dies with
        # "database does not exist". Heal it here (re-run the idempotent provision
        # to recreate the DB) so `provisioned` actually implies the DB exists.
        self._heal_missing_provisioned_db(worktree, resolved_overlay)
        # #2207: abandoned unowned stacks (age-guarded) are reaped first so
        # they neither hold host resources nor distort the stack-cap picture.
        reap_stale_local_stacks(self.stdout.write)
        # #2190: at the cap, reap idle stacks → retry → ENQUEUE (no SystemExit).
        # A queued request (acquire returns False) means the loop's drainer will
        # re-fire ``start`` once a slot frees — so DO NOT advance the FSM here.
        if not acquire_or_enqueue(worktree, write_out=self.stdout.write):
            return worktree.state

        commands = list(resolved_overlay.get_run_commands(worktree))
        with transaction.atomic():
            worktree.start_services(services=commands)
            worktree.save()

        result = WorktreeStartRunner(worktree, overlay=resolved_overlay).run()
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
        *,
        force: bool = typer.Option(
            default=False,
            help="Tear down even when the branch has commits not on any remote (data loss).",
        ),
    ) -> str:
        """Stop docker, drop DB, remove git worktree, delete row.

        Thin wrapper around ``Worktree.teardown()``: the FSM resets to
        CREATED and enqueues ``execute_worktree_teardown``; the runner
        also runs synchronously here so the operator sees immediate
        output. Folds the previous ``teardown`` + ``clean`` commands
        into a single canonical path. Refuses to remove a worktree whose
        branch carries unpushed commits unless ``--force`` is passed.
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
            force=force,
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
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the status as JSON on stdout instead of the human view."),
        ] = False,
    ) -> WorktreeStatus:
        """Report FSM state, ports, the provision report, and the aggregate post-conditions (PR-27).

        A ``provisioned``/``services_up``/``ready`` worktree is only *really*
        provisioned if every aggregate post-condition still holds; when one fails
        (e.g. the env cache or DB was deleted) ``status`` reports it and exits
        non-zero, never claiming green for a rotted provision.
        """
        worktree = resolve_worktree(path)
        ports = get_worktree_ports(compose_project(worktree))
        result: WorktreeStatus = {
            "state": worktree.state,
            "repo_path": worktree.repo_path,
            "branch": worktree.branch,
            "ports": ports,
        }
        summary = _provision_summary(worktree)
        if summary is not None:
            result["provision_report"] = summary
        failures = self._evaluate_provision_post_conditions(worktree, result)
        self.print_result = False
        emit(
            result,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: _render_status(result, stream),
        )
        if failures:
            self.stderr.write(f"  {failures} provision post-condition(s) failed — worktree is not truly provisioned")
            raise SystemExit(1)
        return result

    def _evaluate_provision_post_conditions(self, worktree: Worktree, result: WorktreeStatus) -> int:
        """Populate ``result`` with the aggregate post-conditions; return the failure count.

        Only evaluated for a ``provisioned``/``services_up``/``ready`` worktree —
        a ``created`` row has provisioned nothing to verify.
        """
        provisioned_states = {Worktree.State.PROVISIONED, Worktree.State.SERVICES_UP, Worktree.State.READY}
        if worktree.state not in provisioned_states:
            return 0
        outcomes, failures = evaluate_post_conditions(get_overlay(), worktree)
        result["post_conditions"] = outcomes
        result["provisioned_ok"] = failures == 0
        return failures

    @command()
    def diagnose(
        self,
        path: str = typer.Option("", help="Worktree path (auto-detects from PWD if empty)."),
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the health checklist as JSON on stdout instead of the human view."),
        ] = False,
    ) -> WorktreeDiagnose:
        """Print a structured health checklist for one worktree."""
        worktree = resolve_worktree(path)
        wt_path = (worktree.extra or {}).get("worktree_path", "")
        cache_file = env_cache_path(worktree)

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
        self.print_result = False
        emit(
            checks,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: _render_diagnose(checks, stream),
        )
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

        model_map = {"worktree": Worktree, "ticket": Ticket}
        if model == "task":
            return _task_diagram()
        if model not in model_map:
            self.stderr.write(f"Unknown model: {model}. Choose from: worktree, ticket, task")
            raise SystemExit(1)
        return render_fsm_mermaid(model_map[model])


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

"""Workspace management: create ticket worktrees, finalize, clean stale branches."""

import os
import re
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, TypedDict, cast

import typer
from django.db import transaction
from django_typer.management import TyperCommand, command

from teatree.config import load_config
from teatree.core.cleanup import cleanup_worktree
from teatree.core.management.commands._workspace_cleanup import (
    drop_orphan_databases,
    drop_orphaned_stashes,
    prune_branches,
    resolve_unsynced_worktree,
)
from teatree.core.models import Ticket, Worktree
from teatree.core.orphan_guard import find_orphans_in_workspace
from teatree.core.overlay_loader import get_overlay
from teatree.core.readiness import run_probes
from teatree.core.reconcile import Drift, reconcile_all, reconcile_ticket
from teatree.core.resolve import resolve_worktree
from teatree.core.runners import (
    WorktreeProvisioner,
    WorktreeProvisionRunner,
    WorktreeStartRunner,
    WorktreeTeardownRunner,
)
from teatree.core.worktree_env import write_env_cache
from teatree.utils import git
from teatree.utils.db import drop_db
from teatree.utils.ports import find_free_ports
from teatree.utils.run import CommandFailedError, run_checked

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra


class OrphanEntry(TypedDict):
    repo: str
    branch: str
    status: str
    ahead_count: int


def _warn_orphans(write: Callable[[str], None]) -> None:
    orphans = find_orphans_in_workspace()
    if not orphans:
        return
    preview = orphans[:5]
    write(f"WARNING: {len(orphans)} orphan branch(es) in the workspace:")
    for r in preview:
        write(f"  - {r.repo} ({r.branch}, {r.ahead_count} ahead, {r.status.value})")
    if len(orphans) > len(preview):
        write(f"  - …and {len(orphans) - len(preview)} more")
    write(
        "Run `t3 <overlay> pr ensure-draft --branch <name>` to track them, "
        "or `t3 <overlay> workspace clean-all` to reap synced ones.",
    )


def _workspace_dir() -> Path:
    return load_config().user.workspace_dir


def _fix_drift(drift: Drift) -> list[str]:
    """Apply reconciler fixes for one ticket's drift.

    Each fix uses :func:`run_checked` so failures surface — no silent
    swallow.  Called from ``t3 workspace doctor --fix``.
    """
    fixes: list[str] = []

    for c in drift.orphan_containers:
        run_checked(["docker", "rm", "-f", c.name])
        fixes.append(f"removed orphan container {c.name}")

    for d in drift.orphan_dbs:
        drop_db(d.db_name)
        fixes.append(f"dropped orphan DB {d.db_name}")

    for missing_wt in drift.missing_worktree_dirs:
        Worktree.objects.filter(pk=missing_wt.worktree_pk).update(extra={})
        fixes.append(f"cleared worktree_path on wt#{missing_wt.worktree_pk} (path gone: {missing_wt.path})")

    fixes.extend(
        f"stale worktree dir {stale.path} — remove manually with `git worktree remove`"
        for stale in drift.stale_worktree_dirs
    )

    for missing_cache in drift.missing_env_caches:
        wt = Worktree.objects.get(pk=missing_cache.worktree_pk)
        write_env_cache(wt)
        fixes.append(f"regenerated env cache for wt#{missing_cache.worktree_pk}")

    for cache_drift in drift.env_cache_drifts:
        wt = Worktree.objects.get(pk=cache_drift.worktree_pk)
        write_env_cache(wt)
        fixes.append(f"rewrote drifted env cache for wt#{cache_drift.worktree_pk}")

    fixes.extend(
        f"missing DB {m.db_name} for wt#{m.worktree_pk} — run `t3 <overlay> worktree provision` to re-provision"
        for m in drift.missing_dbs
    )

    return fixes


def _branch_prefix() -> str:
    prefix = os.environ.get("T3_BRANCH_PREFIX", "")
    if not prefix:
        name = git.run(args=["config", "user.name"])
        if name:
            prefix = "".join(word[0].lower() for word in name.split() if word)
    return prefix or "dev"


def _slugify(text: str, max_length: int = 40) -> str:
    """Convert text to a URL-safe slug for branch names."""
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")[:max_length]


def _build_branch_name(repo_names: list[str], ticket_number: str, description: str) -> str:
    """Build the git branch name from repo list, ticket number, and description."""
    prefix = _branch_prefix()
    first_repo = Path(repo_names[0]).name if repo_names else "repo"
    slug = _slugify(description) if description else "ticket"
    return f"{prefix}-{first_repo}-{ticket_number}-{slug}"


class Command(TyperCommand):
    @command()
    def ticket(
        self,
        issue_url: str,
        variant: str = "",
        repos: str = "",
        description: str = "",
    ) -> int:
        """Create or update a ticket and trigger worktree provisioning.

        Thin wrapper around the FSM (BLUEPRINT §4): persist branch + description
        on ``ticket.extra``, advance ``NOT_STARTED → SCOPED → STARTED`` via
        ``scope()`` and ``start()``, and let ``execute_provision`` materialise
        the per-repo git worktrees on the worker side.

        Idempotent: re-running over an already-started ticket merges new repos
        into ``ticket.repos`` so the next ``execute_provision`` picks them up.
        """
        _warn_orphans(self.stderr.write)
        overlay = get_overlay()
        repo_names = [r.strip() for r in repos.split(",") if r.strip()] if repos else overlay.get_workspace_repos()

        with transaction.atomic():
            ticket, _ = Ticket.objects.get_or_create(
                issue_url=issue_url,
                defaults={"variant": variant, "repos": repo_names},
            )

            if ticket.state == Ticket.State.NOT_STARTED:
                ticket.scope(issue_url=issue_url, variant=variant or None, repos=repo_names)

            ticket.repos = list(dict.fromkeys((ticket.repos or []) + repo_names))

            if not description:
                description = overlay.metadata.get_issue_title(issue_url)

            extra = cast("TicketExtra", ticket.extra or {})
            if not extra.get("branch"):
                extra["branch"] = _build_branch_name(repo_names, ticket.ticket_number, description)
            if description and not extra.get("description"):
                extra["description"] = description
            ticket.extra = extra
            ticket.save()

            if ticket.state == Ticket.State.SCOPED:
                ticket.start()
                ticket.save()

        # Run the provisioner synchronously so the CLI gives immediate feedback;
        # the worker that ``start()`` enqueued is idempotent and no-ops when it
        # finds the worktrees already in place. Single source of truth: the runner.
        result = WorktreeProvisioner(ticket).run()

        branch = extra["branch"]
        ticket_dir = _workspace_dir() / branch
        if not result.ok and not ticket.worktrees.exists():
            self.stderr.write(f"  Provisioning failed: {result.detail}")
            ticket.delete()
            with suppress(OSError):
                ticket_dir.rmdir()
            return 0
        if not result.ok:
            self.stderr.write(f"  WARNING: {result.detail}")
        for wt in ticket.worktrees.all():
            self.stdout.write(f"  {wt.repo_path}: worktree #{wt.pk}")

        self.stdout.write(f"\nTicket #{ticket.pk} — worktrees in {ticket_dir}")
        self.stdout.write(f"  Branch: {branch}")
        return int(ticket.pk)

    @command()
    def provision(
        self,
        path: str = typer.Option("", help="Worktree path inside the workspace (auto-detects from PWD)."),
        slow_import: bool = typer.Option(default=False, help="Allow slow DB fallbacks."),  # noqa: FBT001
    ) -> int:
        """Provision every worktree in the current ticket workspace.

        Iterates ``ticket.worktrees`` and fires ``Worktree.provision()``
        for each. Each transition enqueues its worker via on_commit; the
        runner also runs synchronously so the operator gets streaming
        feedback. Stops at the first failure so the operator can fix the
        offending worktree before retrying.
        """
        anchor = resolve_worktree(path)
        ticket = Ticket.objects.get(pk=anchor.ticket.pk)
        overlay = get_overlay()

        worktrees = list(ticket.worktrees.all())
        for wt in worktrees:
            self.stdout.write(f"  Provisioning {wt.repo_path}…")
            with transaction.atomic():
                if wt.state in {Worktree.State.CREATED, Worktree.State.PROVISIONED}:
                    wt.provision()
                    wt.save()
            result = WorktreeProvisionRunner(wt, overlay=overlay, slow_import=slow_import).run()
            self.stdout.write(f"    {result.detail}")
            if not result.ok:
                self.stderr.write(f"  Stopped: {wt.repo_path} failed — fix and re-run.")
                raise SystemExit(1)
        return len(worktrees)

    @command()
    def start(
        self,
        path: str = typer.Option("", help="Worktree path inside the workspace (auto-detects from PWD)."),
    ) -> str:
        """Start docker for every worktree in the current ticket workspace.

        Allocates one shared port set across the workspace, then fires
        ``Worktree.start_services()`` on each worktree (CLI runs the
        runner synchronously). After every worktree starts, runs each
        overlay's readiness probes — exits 1 if any probe fails.
        """
        anchor = resolve_worktree(path)
        ticket = Ticket.objects.get(pk=anchor.ticket.pk)
        overlay = get_overlay()

        ports = find_free_ports(str(load_config().user.workspace_dir))
        self.stdout.write(f"  Ports: {ports}")

        worktrees = list(ticket.worktrees.all())
        failures: list[str] = []
        for wt in worktrees:
            self.stdout.write(f"  Starting {wt.repo_path}…")
            commands = list(overlay.get_run_commands(wt))
            with transaction.atomic():
                wt.start_services(services=commands)
                wt.save()
            result = WorktreeStartRunner(wt, overlay=overlay, ports=ports).run()
            self.stdout.write(f"    {result.detail}")
            if not result.ok:
                failures.append(wt.repo_path)
        if failures:
            self.stderr.write(f"  Failed: {', '.join(failures)}")
            return "error"

        total = 0
        total_failures = 0
        for wt in worktrees:
            probes = overlay.get_readiness_probes(wt)
            if not probes:
                continue
            self.stdout.write(f"  {wt.repo_path}:")
            results = run_probes(probes)
            for r in results:
                self.stdout.write(f"    {r.format()}")
            total += len(results)
            total_failures += sum(1 for r in results if not r.passed)
        if total_failures:
            self.stderr.write(f"  {total_failures} of {total} probe(s) failed")
            raise SystemExit(1)
        return f"started {len(worktrees)} worktree(s)"

    @command()
    def ready(
        self,
        path: str = typer.Option("", help="Worktree path inside the workspace (auto-detects from PWD)."),
    ) -> str:
        """Run readiness probes for every worktree in the ticket workspace.

        Strict: exits 0 iff every probe across every worktree passes. No
        per-worktree skip flag and no env-var escape — if a probe doesn't
        apply to a variant, the overlay's ``get_readiness_probes`` returns
        an empty list (or omits that probe) for that worktree.
        """
        anchor = resolve_worktree(path)
        ticket = Ticket.objects.get(pk=anchor.ticket.pk)
        overlay = get_overlay()

        worktrees = list(ticket.worktrees.all())
        total = 0
        total_failures = 0
        for wt in worktrees:
            probes = overlay.get_readiness_probes(wt)
            if not probes:
                self.stdout.write(f"  {wt.repo_path}: no probes")
                continue
            self.stdout.write(f"  {wt.repo_path}:")
            results = run_probes(probes)
            for r in results:
                self.stdout.write(f"    {r.format()}")
            failures = [r for r in results if not r.passed]
            total += len(results)
            total_failures += len(failures)
        if total_failures:
            self.stderr.write(f"  {total_failures} of {total} probe(s) failed")
            raise SystemExit(1)
        return "ok"

    @command()
    def teardown(
        self,
        path: str = typer.Option("", help="Worktree path inside the workspace (auto-detects from PWD)."),
    ) -> str:
        """Tear down every worktree in the current ticket workspace.

        Fires ``Worktree.teardown()`` on each worktree. Continues past
        per-worktree failures to maximise cleanup; surfaces them in the
        final summary.
        """
        anchor = resolve_worktree(path)
        ticket = Ticket.objects.get(pk=anchor.ticket.pk)

        worktrees = list(ticket.worktrees.all())
        labels: list[str] = []
        failures: list[str] = []
        for wt in worktrees:
            repo = wt.repo_path
            # Snapshot before the transition body resets db_name/extra
            snapshot_db_name = wt.db_name
            snapshot_extra = wt.get_extra()
            with transaction.atomic():
                wt.teardown()
                wt.save()
            result = WorktreeTeardownRunner(
                wt,
                snapshot_db_name=snapshot_db_name,
                snapshot_extra=snapshot_extra,
            ).run()
            if result.ok:
                labels.append(result.detail)
            else:
                failures.append(f"{repo}: {result.detail}")
        for label in labels:
            self.stdout.write(f"  {label}")
        if failures:
            for failure in failures:
                self.stderr.write(f"  {failure}")
            return f"completed with {len(failures)} failure(s)"
        return f"tore down {len(worktrees)} worktree(s)"

    @command()
    def finalize(self, ticket_id: int, *, message: str = "") -> str:
        """Squash worktree commits into one, then rebase on the default branch."""
        ticket = Ticket.objects.get(pk=ticket_id)
        results: list[str] = []
        for worktree in ticket.worktrees.all():
            repo = worktree.repo_path
            repo_dir = (worktree.extra or {}).get("worktree_path") or repo
            default_br = git.default_branch(repo)
            try:
                status = git.status_porcelain(repo_dir)
                if status:
                    results.append(f"{repo}: SKIPPED — uncommitted changes:\n{status}")
                    continue

                git.fetch(repo_dir, "origin", default_br)

                base = git.merge_base(repo_dir, f"origin/{default_br}")
                count = git.rev_count(repo_dir, f"{base}..HEAD")
                log = git.log_oneline(repo_dir, f"{base}..HEAD")
                if log:
                    self.stdout.write(f"  {repo} commits ({count}):\n    " + "\n    ".join(log.splitlines()))

                if count > 1:
                    if not message:
                        message = log.splitlines()[0] if log else f"Squash {count} commits"
                    git.soft_reset(repo_dir, base)
                    git.commit(repo_dir, message)
                    results.append(f"{repo}: squashed {count} commits")
                else:
                    results.append(f"{repo}: single commit, no squash needed")

                git.rebase(repo_dir, f"origin/{default_br}")
                results.append(f"{repo}: rebased on {default_br}")
            except CommandFailedError as exc:
                results.extend(
                    [
                        f"{repo}: rebase failed — {exc}",
                        f"  To abort: git -C {repo_dir} rebase --abort",
                        f"  To resolve: fix conflicts, git add, then: git -C {repo_dir} rebase --continue",
                    ]
                )
        return "\n".join(results)

    @command(name="clean-merged")
    def clean_merged(self) -> list[str]:
        """Tear down every worktree whose ticket is already MERGED.

        On-demand reconciler for the daily followup sync. Use when merged-MR
        cleanup silently failed and stale docker containers, branches, or
        databases linger. Errors are surfaced inline — no suppression.
        """
        cleaned: list[str] = []
        merged_tickets = Ticket.objects.filter(state=Ticket.State.MERGED)
        for ticket in merged_tickets:
            worktrees = list(Worktree.objects.filter(ticket=ticket))
            if not worktrees:
                continue
            for wt in worktrees:
                try:
                    cleaned.append(cleanup_worktree(wt, force=True))
                except RuntimeError as exc:
                    cleaned.append(f"FAILED {wt.repo_path} ({wt.branch}): {exc}")
        if not cleaned:
            return ["No merged tickets have lingering worktrees."]
        return cleaned

    @command()
    def doctor(
        self,
        ticket: Annotated[int, typer.Option(help="Reconcile just this ticket pk; 0 = all tickets.")] = 0,
        *,
        fix: Annotated[bool, typer.Option(help="Apply fixes instead of just listing drift.")] = False,
    ) -> list[str]:
        """Detect state drift across every store; optionally fix it.

        Checks Django ↔ git worktrees, Postgres DBs, docker containers, redis
        slots, env cache files.  Without ``--fix`` prints drift; with
        ``--fix`` cleans orphan containers, drops orphan DBs, regenerates
        missing env caches, and prunes stale worktree dirs.  Every action
        uses :func:`run_checked` — no silent swallow.
        """
        if ticket:
            drifts = {ticket: reconcile_ticket(Ticket.objects.get(pk=ticket))}
            if not drifts[ticket].has_drift:
                drifts = {}
        else:
            drifts = reconcile_all()

        if not drifts:
            return ["No drift detected."]

        lines: list[str] = []
        for ticket_pk, drift in sorted(drifts.items()):
            lines.append(f"Ticket #{ticket_pk}:")
            lines.extend(f"  {finding}" for finding in drift.format().splitlines())
            if fix:
                lines.extend(f"  [fix] {msg}" for msg in _fix_drift(drift))
        if not fix:
            lines.extend(("", "Rerun with --fix to apply fixes."))
        return lines

    @command(name="list-orphans")
    def list_orphans(self) -> list[OrphanEntry]:
        """List orphan branches (commits ahead of origin/main AND no open PR) across the workspace.

        Used by the session-end hook and the ``workspace ticket`` warning to
        surface work that would otherwise be lost when a session closes or a
        new worktree is created. Emits a JSON-serialisable list — one entry
        per orphan.
        """
        return [
            OrphanEntry(repo=r.repo, branch=r.branch, status=r.status.value, ahead_count=r.ahead_count)
            for r in find_orphans_in_workspace()
        ]

    @command(name="clean-all")
    def clean_all(
        self,
        keep_dslr: int = typer.Option(1, help="Number of DSLR snapshots to keep per tenant."),
    ) -> list[str]:
        """Prune merged worktrees, stale branches, orphaned stashes, orphan databases, and old DSLR snapshots."""
        workspace = _workspace_dir()
        cleaned: list[str] = []
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        for wt in Worktree.objects.filter(state=Worktree.State.CREATED):
            try:
                cleaned.append(cleanup_worktree(wt))
            except RuntimeError as exc:
                cleaned.append(resolve_unsynced_worktree(wt, exc, interactive=interactive))

        for entry in workspace.iterdir():
            if entry.is_dir() and not any(entry.iterdir()):
                with suppress(OSError):
                    entry.rmdir()
                    cleaned.append(f"Removed empty dir: {entry.name}")

        cleaned.extend(drop_orphan_databases())

        repo_root = Path.cwd()
        if (repo_root / ".git").exists():
            cleaned.extend(prune_branches(str(repo_root)))
            cleaned.extend(drop_orphaned_stashes(str(repo_root)))

        # Prune old DSLR snapshots
        from teatree.utils.django_db import prune_dslr_snapshots  # noqa: PLC0415

        pruned = prune_dslr_snapshots(keep=keep_dslr)
        cleaned.extend(f"Pruned DSLR snapshot: {name}" for name in pruned)

        return cleaned

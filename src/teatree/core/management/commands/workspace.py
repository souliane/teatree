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
from django_fsm import can_proceed
from django_typer.management import TyperCommand, command

from teatree.config import load_config
from teatree.core.cleanup import cleanup_worktree
from teatree.core.dev_repo import resolve_repo_names
from teatree.core.local_stack_gate import refuse_if_limit_exceeded
from teatree.core.management.commands import _workspace_helpers as _wh
from teatree.core.management.commands._workspace_cleanup import (
    WorktreeReaper,
    _die,
    _fix_drift,
    _raise_on_cleanup_failures,
    drop_orphan_databases,
    drop_orphaned_stashes,
    is_clean_ignored,
    prune_branches,
    resolve_unsynced_worktree,
)
from teatree.core.management.commands._workspace_docker import reap_orphan_worktree_docker
from teatree.core.models import Ticket, Worktree
from teatree.core.models.ticket import format_intake_summary
from teatree.core.orphan_guard import find_orphans_in_workspace
from teatree.core.overlay_loader import get_overlay
from teatree.core.public_identity import StampResult, is_public_github_remote, set_local_noreply_identity
from teatree.core.readiness import run_and_report_probes
from teatree.core.reconcile import reconcile_all, reconcile_ticket
from teatree.core.resolve import WorktreeNotFoundError, _get_user_cwd, resolve_worktree
from teatree.core.runners import (
    WorktreeProvisioner,
    WorktreeProvisionRunner,
    WorktreeStartRunner,
    WorktreeTeardownRunner,
)
from teatree.utils import git
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra
    from teatree.core.overlay import OverlayBase


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
        "Run `t3 <overlay> pr ensure-pr --branch <name>` to track them, "
        "or `t3 <overlay> workspace clean-all` to reap synced ones.",
    )


def _workspace_dir() -> Path:
    return load_config().user.workspace_dir


def _resolve_workspace_ticket(path: str) -> Ticket:
    """Resolve the ticket for a workspace-scoped command.

    Workspace commands (provision/start/ready/teardown) act on *every*
    worktree in a ticket, so they should be runnable both from inside a
    worktree subdir and from the ticket workspace root that holds those
    subdirs. First try the normal worktree resolution; if that fails
    because we're at the workspace root, match child worktree dirs back
    to their ticket.
    """
    try:
        anchor = resolve_worktree(path)
        return Ticket.objects.get(pk=anchor.ticket.pk)
    except WorktreeNotFoundError:
        base = Path(path).resolve() if path else Path(_get_user_cwd()).resolve()
        ticket_pks: set[int] = set()
        for wt in Worktree.objects.exclude(extra__worktree_path__isnull=True):
            wt_path = (wt.extra or {}).get("worktree_path", "")
            if wt_path and Path(wt_path).resolve().parent == base:
                ticket_pks.add(wt.ticket_id)
        if len(ticket_pks) == 1:
            return Ticket.objects.get(pk=ticket_pks.pop())
        if len(ticket_pks) > 1:
            msg = (
                f"{base} holds worktrees from multiple tickets ({sorted(ticket_pks)}).\n"
                "Run the command from a specific worktree subdir."
            )
            raise WorktreeNotFoundError(msg) from None
        raise


def _report_worktree_probes(
    worktrees: list[Worktree],
    overlay: "OverlayBase",
    write: Callable[[str], None],
    *,
    note_empty: bool,
) -> tuple[int, int]:
    """Run each worktree's readiness probes; return ``(total, failures)``.

    Shared by ``start`` (probe only the worktrees that started) and
    ``ready`` (probe every worktree). ``note_empty`` reports a worktree
    with no probes explicitly (``ready``) or skips it silently (``start``).
    """
    total = 0
    total_failures = 0
    for wt in worktrees:
        probes = overlay.get_readiness_probes(wt)
        if not probes:
            if note_empty:
                write(f"  {wt.repo_path}: no probes")
            continue
        write(f"  {wt.repo_path}:")
        summary = run_and_report_probes(probes, write_line=write, indent="    ")
        total += summary.total
        total_failures += summary.failures
    return total, total_failures


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


def _locked_get_or_create_ticket(issue_url: str, variant: str, repo_names: list[str]) -> Ticket:
    """Get-or-create the ticket and lock it for the provisioning RMW.

    #800 N3: ``get_or_create`` does not lock the row; the subsequent
    ``scope()`` + ``repos`` + ``extra`` + full ``save()`` is a
    read-modify-write that a concurrent provisioner for the same
    ``issue_url`` would lost-update. On an existing row we re-fetch it
    ``select_for_update``-locked (the ``ensure_session()`` pattern,
    ``ticket.py``); a freshly-created row is already exclusive to this
    transaction. Caller must be inside ``transaction.atomic()``.
    """
    ticket, created = Ticket.objects.get_or_create(
        issue_url=issue_url,
        defaults={"variant": variant, "repos": repo_names},
    )
    if created:
        return ticket
    return Ticket.objects.select_for_update().get(pk=ticket.pk)


def _build_branch_name(repo_names: list[str], ticket_number: str, description: str) -> str:
    """Build the flat ``<number>-<slug>`` branch name; legacy initials/repo prefix dropped (#1323)."""
    del repo_names
    slug = _slugify(description) if description else "ticket"
    return f"{ticket_number}-{slug}"


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
        # #1310: a multi-overlay install with ``T3_OVERLAY_NAME`` missing
        # used to die on the ambiguous ``get_overlay()`` call here.
        # Infer from the issue URL whose workspace repos own it; the
        # default ``get_overlay()`` env-var path still wins when set.
        overlay = get_overlay(_wh.resolve_overlay_name_for_url(issue_url))
        repo_names = resolve_repo_names(overlay, issue_url, repos)

        with transaction.atomic():
            ticket = _locked_get_or_create_ticket(issue_url, variant, repo_names)

            # Refuse a silent rebind when --variant disagrees with the existing ticket's variant (#1306).
            _wh.reject_variant_mismatch(self.stderr.write, ticket, variant)

            if ticket.state == Ticket.State.NOT_STARTED:
                ticket.scope(issue_url=issue_url, variant=variant or None, repos=repo_names)

            ticket.repos = list(dict.fromkeys((ticket.repos or []) + repo_names))

            if not description:
                description = overlay.get_issue_title(issue_url)

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

            # #748: every entry point converges on a durable session so
            # the shipping gate has a phase-attestation home regardless
            # of which path created the ticket.
            ticket.ensure_session()

        # Run the provisioner synchronously so the CLI gives immediate feedback;
        # the worker that ``start()`` enqueued is idempotent and no-ops when it
        # finds the worktrees already in place. Single source of truth: the runner.
        result = WorktreeProvisioner(ticket).run()

        branch = extra["branch"]
        ticket_dir = _workspace_dir() / branch
        if not result.ok and not ticket.worktrees.exists():  # ty: ignore[unresolved-attribute]
            self.stderr.write(f"  Provisioning failed: {result.detail}")
            # #748: only discard the ticket if it carries NO phase
            # attestation. ``get_or_create`` may have resolved an
            # existing loop/coordinator-built ticket whose sessions hold
            # genuinely-completed-work phase records; ``Session.ticket``
            # is ``on_delete=CASCADE``, so ``ticket.delete()`` here would
            # cascade-reap that attestation (the observed session-reaper).
            # A failed provision must never destroy attested work — leave
            # the ticket + sessions intact and just report the failure.
            visited, _ = ticket.aggregate_phase_records()
            if not visited:
                ticket.delete()
                with suppress(OSError):
                    ticket_dir.rmdir()
            return 0
        if not result.ok:
            self.stderr.write(f"  WARNING: {result.detail}")
        self.stdout.write(format_intake_summary(ticket, str(ticket_dir), branch))
        return int(ticket.pk)

    @command()
    def provision(
        self,
        ticket_id: int = typer.Argument(0, help="Optional ticket id (alias for PWD auto-detect; #941)."),
        path: str = typer.Option("", help="Worktree path inside the workspace (auto-detects from PWD)."),
        slow_import: bool = typer.Option(default=False, help="Allow slow DB fallbacks."),  # noqa: FBT001
    ) -> int:
        """Provision every worktree in the current ticket workspace.

        Iterates ``ticket.worktrees`` and fires ``Worktree.provision()``
        for each. Stops at the first failure so the operator can fix
        the offending worktree before retrying. #941: an optional
        positional ``ticket_id`` is a no-op alias for PWD auto-detect
        (agents typed ``provision <id>`` from habit; typer used to reject it with rc=1).
        """
        ticket = Ticket.objects.filter(pk=ticket_id).first() if ticket_id else None
        if ticket is None:
            ticket = _resolve_workspace_ticket(path)
        # #1310: disambiguate from ``ticket.overlay`` so multi-overlay
        # installs don't die on ambiguous ``get_overlay()`` when
        # ``T3_OVERLAY_NAME`` env var is missing (a real path when a
        # caller bypasses the CLI bridge or the env is lost).
        overlay = get_overlay(ticket.overlay or None)

        worktrees = list(Worktree.objects.filter(ticket=ticket))
        for wt in worktrees:
            self.stdout.write(f"  Provisioning {wt.repo_path}…")
            with transaction.atomic():
                if wt.state in {Worktree.State.CREATED, Worktree.State.PROVISIONED}:
                    wt.provision()
                    wt.save()
            result = WorktreeProvisionRunner(wt, overlay=overlay, slow_import=slow_import).run()
            self.stdout.write(f"    {result.detail}")
            if not result.ok:
                _die(self.stderr.write, f"  Stopped: {wt.repo_path} failed — fix and re-run.")
        return len(worktrees)

    @command()
    def start(
        self,
        path: str = typer.Option("", help="Worktree path inside the workspace (auto-detects from PWD)."),
    ) -> str:
        """Start docker for every worktree in the current ticket workspace.

        Fires ``Worktree.start_services()`` on each worktree (CLI runs the
        runner synchronously). Each runner brings up docker-compose, which
        auto-maps host ports; the actual ports are then queried via
        ``docker compose port`` and stored on ``Worktree.extra["ports"]``.
        After every worktree starts, runs each overlay's readiness probes —
        exits 1 if any probe fails.
        """
        ticket = _resolve_workspace_ticket(path)
        # #1310: disambiguate from ``ticket.overlay`` (see ``provision``).
        overlay = get_overlay(ticket.overlay or None)

        worktrees = list(Worktree.objects.filter(ticket=ticket))
        started: list[Worktree] = []
        failures: list[str] = []
        refuse_if_limit_exceeded(next(iter(worktrees), None), write_err=self.stderr.write)
        for wt in worktrees:
            # The worktrees in one ticket can be in different FSM states
            # (e.g. a sibling repo whose provision has not run yet is still
            # CREATED). ``start_services`` only accepts the
            # ``[PROVISIONED, SERVICES_UP, READY]`` source states; firing it
            # on a CREATED worktree raises ``TransitionNotAllowed`` and would
            # crash the whole command, abandoning the worktrees already
            # started. Skip the ones that can't transition and start the rest.
            if not can_proceed(wt.start_services):
                self.stdout.write(f"  Skipping {wt.repo_path} (state: {wt.state}, not ready to start)")
                continue
            self.stdout.write(f"  Starting {wt.repo_path}…")
            commands = list(overlay.get_run_commands(wt))
            with transaction.atomic():
                wt.start_services(services=commands)
                wt.save()
            started.append(wt)
            result = WorktreeStartRunner(wt, overlay=overlay).run()
            self.stdout.write(f"    {result.detail}")
            if not result.ok:
                failures.append(wt.repo_path)
        if failures:
            _die(self.stderr.write, f"  Failed: {', '.join(failures)}")

        total, total_failures = _report_worktree_probes(started, overlay, self.stdout.write, note_empty=False)
        if total_failures:
            _die(self.stderr.write, f"  {total_failures} of {total} probe(s) failed")
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
        ticket = _resolve_workspace_ticket(path)
        # #1310: disambiguate from ``ticket.overlay`` (see ``provision``).
        overlay = get_overlay(ticket.overlay or None)

        worktrees = list(Worktree.objects.filter(ticket=ticket))
        total, total_failures = _report_worktree_probes(worktrees, overlay, self.stdout.write, note_empty=True)
        if total_failures:
            _die(self.stderr.write, f"  {total_failures} of {total} probe(s) failed")
        return "ok"

    @command()
    def teardown(
        self,
        path: str = typer.Option("", help="Worktree path inside the workspace (auto-detects from PWD)."),
        *,
        force: bool = typer.Option(
            default=False,
            help="Tear down even when a branch has commits not on any remote (data loss).",
        ),
    ) -> str:
        """Tear down every worktree in the current ticket workspace.

        Fires ``Worktree.teardown()`` on each worktree. Continues past
        per-worktree failures to maximise cleanup; surfaces them in the
        final summary. Refuses to remove a worktree whose branch carries
        unpushed commits unless ``--force`` is passed.
        """
        ticket = _resolve_workspace_ticket(path)

        worktrees = list(Worktree.objects.filter(ticket=ticket))
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
                force=force,
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
                self.stderr.write(f"  Teardown failed — {failure}")
            raise SystemExit(1)
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
                    message = message or (log.splitlines()[0] if log else f"Squash {count} commits")
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

        On-demand reconciler for the daily followup sync. Use when merged-PR
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
                    cleaned.append(str(cleanup_worktree(wt, strict_hygiene=False)))
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

    @command(name="stamp-identity")
    def stamp_identity(self, repo: str = ".") -> StampResult:
        """Stamp the scoped noreply git identity onto an existing souliane clone (#762).

        Fixes public souliane/* clones/worktrees created before the
        provisioner source-fix (new worktrees are stamped at creation).
        Idempotent. Refuses non-souliane / private remotes so the private overlay's
        legitimate real-identity attribution is never touched.
        """
        slug = git.remote_slug(repo)
        if not is_public_github_remote(slug):
            return StampResult(
                stamped=False,
                reason=f"not a public GitHub remote (slug={slug!r}) — noreply-identity stamping not required",
            )
        set_local_noreply_identity(repo)
        return StampResult(stamped=True, repo=repo, slug=slug)

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
        """Prune merged worktrees, stale branches, stashes, orphan databases + docker, and old DSLR snapshots."""
        workspace = _workspace_dir()
        cleaned: list[str] = []
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        in_use = _wh.dslr_tenants_in_use()  # before cleanup loop reaps CREATED worktrees (#1306)
        reaper = WorktreeReaper(workspace)
        cleaned.extend(reaper.reap_squash_merged_worktrees(interactive=interactive))
        for wt in Worktree.objects.filter(state=Worktree.State.CREATED):
            if is_clean_ignored(wt.branch, overlay=wt.overlay):
                cleaned.append(f"SKIPPED '{wt.branch}': matches clean_ignore — keeping")
                continue
            try:
                cleaned.append(str(cleanup_worktree(wt)))
            except RuntimeError as exc:
                cleaned.append(resolve_unsynced_worktree(wt, exc, interactive=interactive))

        cleaned.extend(reaper.remove_empty_ticket_dirs())

        cleaned.extend(drop_orphan_databases())
        cleaned.extend(reap_orphan_worktree_docker())

        repo_root = Path.cwd()
        if (repo_root / ".git").exists():
            cleaned.extend(prune_branches(str(repo_root)))
            cleaned.extend(drop_orphaned_stashes(str(repo_root)))

        cleaned.extend(_wh.prune_dslr_snapshots_skipping(keep=keep_dslr, in_use_tenants=in_use))

        _raise_on_cleanup_failures(cleaned, self.stdout.write, self.stderr.write)
        return cleaned

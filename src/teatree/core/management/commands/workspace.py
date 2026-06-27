"""Workspace management: create ticket worktrees, finalize, clean stale branches."""

import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, TypedDict, cast

import typer
from django.db import transaction
from django_fsm import can_proceed
from django_typer.management import TyperCommand, command

from teatree.config import load_config
from teatree.core.gates.local_stack_gate import acquire_or_enqueue
from teatree.core.gates.orphan_guard import find_orphans_in_workspace
from teatree.core.management.commands import _workspace_helpers as _wh
from teatree.core.management.commands._workspace_clean_all import CleanAllIO, run_clean_all
from teatree.core.management.commands._workspace_cleanup import _die, _fix_drift
from teatree.core.management.commands._workspace_docker import reap_stale_local_stacks, reap_stale_report
from teatree.core.management.commands._workspace_finalize import run_finalize
from teatree.core.management.commands._workspace_landscape import LandscapeReport, run_landscape
from teatree.core.management.commands._workspace_salvage import emit_records_json, run_salvage
from teatree.core.management.commands._workspace_ticket_intake import (
    ForeignIssueWorktreeRefusedError,
    RawTicketInputs,
    build_intake,
    build_ticket,
)
from teatree.core.models import Ticket, Worktree
from teatree.core.models.ticket_display import format_intake_summary
from teatree.core.overlay_loader import get_overlay
from teatree.core.public_identity import StampResult, is_public_github_remote, set_local_noreply_identity
from teatree.core.readiness import run_and_report_probes
from teatree.core.reconcile import reconcile_all, reconcile_ticket
from teatree.core.resolve import WorktreeNotFoundError, _get_user_cwd, resolve_worktree, workspace_owner_ticket
from teatree.core.runners import (
    WorktreeProvisioner,
    WorktreeProvisionRunner,
    WorktreeStartRunner,
    WorktreeTeardownRunner,
)
from teatree.core.worktree_done import reap_done_worktrees
from teatree.docker.reclaim import reclaim_disk
from teatree.utils import git

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
    because we're at the workspace root, attribute the workspace dir to its
    owning ticket through the single fail-loud resolver
    (:func:`workspace_owner_ticket`) — the same symlink-tolerant, multi-owner
    policy the auto-register chain uses, never a second hand-rolled check.
    """
    try:
        anchor = resolve_worktree(path)
        return Ticket.objects.get(pk=anchor.ticket.pk)
    except WorktreeNotFoundError:
        base = Path(path).resolve() if path else Path(_get_user_cwd()).resolve()
        owner = workspace_owner_ticket(base)
        if owner is None:
            raise
        return Ticket.objects.get(pk=owner.pk)


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


class Command(TyperCommand):
    @command()
    def ticket(
        self,
        issue_url: str,
        variant: str = "",
        repos: str = "",
        description: str = "",
        *,
        take_over: Annotated[
            bool,
            typer.Option(
                "--take-over",
                help="Proceed even when another worktree dir for this issue already exists (#2217).",
            ),
        ] = False,
    ) -> int:
        """Create or update a ticket and trigger worktree provisioning.

        Thin wrapper around the FSM (BLUEPRINT §4): persist branch + description
        on ``ticket.extra``, advance ``NOT_STARTED → SCOPED → STARTED`` via
        ``scope()`` and ``start()``, and let ``execute_provision`` materialise
        the per-repo git worktrees on the worker side.

        Idempotent: re-running over an already-started ticket merges new repos
        into ``ticket.repos`` so the next ``execute_provision`` picks them up.
        Per-repo branches (#33): a ``--repos`` token may carry its branch as
        ``repo:branch`` so split-branch repos provision as siblings in one dir
        (the dir is ``extra['branch']``; a bare token falls back to it).

        Filesystem-evidence double-dispatch guard (#2217): before materialising a
        worktree for issue ``N``, refuse when a *foreign* ``N-*`` worktree dir
        already exists (someone may already be on it) unless ``--take-over`` is
        passed. Re-provisioning the ticket's own existing dir is always allowed.
        """
        _warn_orphans(self.stderr.write)
        # #1310: a multi-overlay install with ``T3_OVERLAY_NAME`` missing
        # used to die on the ambiguous ``get_overlay()`` call here.
        # Infer from the issue URL whose workspace repos own it; the
        # default ``get_overlay()`` env-var path still wins when set.
        overlay = get_overlay(_wh.resolve_overlay_name_for_url(issue_url))
        raw = RawTicketInputs(issue_url, repos, variant, description, take_over)
        intake = build_intake(overlay, raw)
        try:
            ticket = build_ticket(self.stderr.write, overlay, intake, _workspace_dir())
        except ForeignIssueWorktreeRefusedError:
            return 0

        branch = cast("TicketExtra", ticket.extra)["branch"]
        ticket_dir = _workspace_dir() / branch

        # Run the provisioner synchronously so the CLI gives immediate feedback;
        # the worker that ``start()`` enqueued is idempotent and no-ops when it
        # finds the worktrees already in place. Single source of truth: the runner.
        result = WorktreeProvisioner(ticket).run()
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

        # #2207: free abandoned unowned stacks (age-guarded) before the heavy
        # provisioning work competes with them for host CPU/RAM.
        reap_stale_local_stacks(self.stdout.write)

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
        # #2207: abandoned unowned stacks (age-guarded) are reaped first so
        # they neither hold host resources nor distort the stack-cap picture.
        reap_stale_local_stacks(self.stdout.write)
        # #2190: at the cap, reap idle stacks → retry → ENQUEUE (no SystemExit).
        # A queued request means the loop's drainer re-fires ``start`` once a
        # slot frees — DO NOT advance any worktree's FSM for this ticket.
        if not acquire_or_enqueue(next(iter(worktrees), None), write_out=self.stdout.write):
            return f"queued {len(worktrees)} worktree(s) — no free local-stack slot"
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
            # #1038: heal a sibling whose interrupted provision left no DB so the
            # multi-repo start doesn't die on "database does not exist". Skip only
            # the worktree whose heal failed — never abort the whole ticket.
            if _wh.heal_db_or_record_failure(wt, overlay, failures, self.stdout.write):
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
        return run_finalize(ticket, message=message, write=self.stdout.write)

    @command(name="clean-merged")
    def clean_merged(self) -> list[str]:
        """Tear down every done worktree (analyze-then-wipe) on demand.

        On-demand reconciler for the daily followup sync — the same consolidated
        done+redundant reaper ``clean-all`` and the FSM teardown use. Use when
        merged-PR cleanup silently failed and stale docker stacks, branches, or
        databases linger. A not-done or potentially-needed worktree is KEPT with a
        reported reason; nothing unproven is destroyed.
        """
        return reap_done_worktrees(_workspace_dir(), dry_run=False)

    @command()
    def doctor(
        self,
        ticket: Annotated[int, typer.Option(help="Reconcile just this ticket pk; 0 = all tickets.")] = 0,
        *,
        fix: Annotated[bool, typer.Option(help="Apply fixes instead of just listing drift.")] = False,
    ) -> list[str]:
        """Detect state drift across every store; optionally fix it.

        Checks Django ↔ git worktrees, Postgres DBs, docker containers,
        env cache files.  Without ``--fix`` prints drift; with
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
        Idempotent. Refuses non-github / private remotes so a private
        overlay's (or a GitLab clone's) legitimate real-identity
        attribution is never touched.
        """
        # #2655: the visibility gate must see the FULL remote URL (host
        # intact) — a host-stripped slug would resolve a GitLab clone's
        # bare ``owner/repo`` against github.com. ``slug`` is kept only
        # for the human-readable result.
        url = git.remote_url(repo)
        slug = git.remote_slug(repo)
        if not is_public_github_remote(url):
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

    @command()
    def landscape(self) -> LandscapeReport:
        """Survey what is already in flight or settled before planning (#2541).

        The intake landscape survey the ``/t3:ticket`` step runs and the planner
        consumes: the operator's open PRs/MRs, the local worktrees carrying
        uncommitted or unpushed work, and a per-issue close/merge/supersede
        recommendation against the in-flight PR landscape. Forge or git probes
        that cannot complete degrade to ``warnings`` rather than aborting — a
        missed in-flight branch is worse than a noisy warning. Emits a
        JSON-serialisable survey so the planner plans against reality instead of
        re-deriving it.
        """
        return run_landscape(_workspace_dir())

    @command(name="reap-stale")
    def reap_stale(
        self,
        min_age_minutes: int = typer.Option(
            0,
            help="Override the stale threshold (minutes); 0 uses the configured stale_stack_min_age_minutes.",
        ),
        dry_run: bool = typer.Option(default=False, help="List the stacks that would be reaped without removing."),  # noqa: FBT001 — CLI flag
    ) -> list[str]:
        """Tear down ABANDONED docker stacks no live worktree owns (age-guarded, #2207).

        The on-demand twin of the automatic pre-start/pre-provision sweep: an
        unowned compose project is reaped only when its newest container
        lifecycle event is older than the threshold, so a parallel session's
        fresh hand-rolled stack is never touched. ``clean-all`` remains the
        blunt deep clean (every unowned project, regardless of age).
        """
        return reap_stale_report(min_age_minutes=min_age_minutes, dry_run=dry_run, write_out=self.stdout.write)

    @command(name="reclaim-disk")
    def reclaim_disk_cmd(
        self,
        dry_run: bool = typer.Option(default=False, help="Plan the reclaim set without removing anything."),  # noqa: FBT001 — CLI flag
    ) -> str:
        """Free disk via the three safe Docker prunes, then STOP — engine: ``teatree.docker.reclaim`` (#2246)."""
        return reclaim_disk(dry_run=dry_run).render()

    @command(name="clean-all")
    def clean_all(
        self,
        keep_dslr: int = typer.Option(1, help="Number of DSLR snapshots to keep per tenant."),
        *,
        dry_run: bool = typer.Option(
            default=False,
            help="Preview only: list each worktree that WOULD WIPE (with its done-signal source) "
            "or be KEPT, removing nothing.",
        ),
    ) -> list[str]:
        """Reap every done+redundant worktree, then prune branches/stashes, orphan DBs/docker/env-roots, DSLR.

        The consolidated done-worktree reaper runs first: a worktree is wiped only
        when its ticket is done (MERGED/DELIVERED/IGNORED, or a forge squash-merge)
        AND every unpushed commit and uncommitted change is PROVEN redundant. A
        not-done or potentially-needed worktree is KEPT with a reported reason — the
        #706 data-loss guard, surfaced as the primary analyze-before-wipe step.
        There is no recovery snapshot: unproven work is kept, never destroyed.

        Fully unattended (#2361 / CORRECTION 3): never blocks on stdin and never
        prompts — an uncertain worktree is kept with a warning, salvage is the
        separate explicit ``t3 <overlay> pr create``. ``--dry-run`` previews the
        reaper (would-wipe/keep) and removes nothing.

        The ordered passes live in :func:`run_clean_all`; this method is the thin
        CLI wrapper that supplies the worktree dir and the command's IO sinks.
        """
        return run_clean_all(
            _workspace_dir(),
            CleanAllIO(write_out=self.stdout.write, write_err=self.stderr.write),
            keep_dslr=keep_dslr,
            dry_run=dry_run,
        )

    @command(name="emit")
    def emit(self) -> str:
        """Print the machine-readable JSON handoff for every NOT-auto-deleted item (#2763).

        The read-only structured EMIT the judgment skill consumes: a JSON array of
        records (path, branch, kind, unique_commit_shas, merged_with_post_merge_work,
        banned_terms_status, liveness, last_commit_date, owner — schema in
        ``teatree.core.cleanup_emit``). Removes nothing — ``clean-all`` does the
        auto-deletion of provably-redundant items; this surfaces the rest for the
        skill to route (superseded / salvage-to-fresh-PR / defer-live).
        """
        rendered = emit_records_json(_workspace_dir())
        self.stdout.write(rendered)
        return rendered

    @command(name="salvage")
    def salvage(
        self,
        source_ref: str,
        *,
        salvage_branch: str = typer.Option("", help="Fresh branch to capture onto (default: salvage/<source_ref>)."),
        target: str = typer.Option("origin/main", help="Base the salvage PR opens against."),
        allow_banned: bool = typer.Option(
            default=False, help="Skip the final banned-terms safety gate (the skill cleaned the content)."
        ),
    ) -> str:
        """Capture a branch's unique content to a PR, verify it landed, then delete the branch (#2763).

        The salvage primitive the judgment skill calls once it has decided an
        emitted item is worth keeping and cleaned any banned terms. Fail-safe: the
        source branch is deleted ONLY after the forge confirms the PR — a failed
        push / open / verify leaves it intact. Operates on the current repo (cwd).
        """
        line = run_salvage(source_ref, salvage_branch=salvage_branch, target=target, allow_banned=allow_banned)
        self.stdout.write(line)
        return line

"""Workspace management: create ticket worktrees, finalize, clean stale branches."""

from pathlib import Path
from typing import IO, Annotated, cast

import typer
from django.db import transaction
from django_fsm import can_proceed
from django_typer.management import TyperCommand, command

from teatree.config import worktree_root as _config_worktree_root
from teatree.core.cleanup.unshipped_restore import restore_bundle
from teatree.core.gates.local_stack_gate import acquire_or_enqueue
from teatree.core.gates.open_pr_teardown_gate import check_no_open_prs
from teatree.core.intake.issue_ref import InvalidIssueRefError, canonicalize_issue_ref
from teatree.core.machine_output import emit
from teatree.core.management.commands._workspace import helpers as _wh
from teatree.core.management.commands._workspace.anchor import resolve_workspace_ticket
from teatree.core.management.commands._workspace.clean_all import CleanAllIO, run_clean_all
from teatree.core.management.commands._workspace.cleanup import _die
from teatree.core.management.commands._workspace.dead_rows import build_dead_row_report, write_dead_row_lines
from teatree.core.management.commands._workspace.docker import reap_stale_local_stacks, reap_stale_report
from teatree.core.management.commands._workspace.drift_report import run_drift_report
from teatree.core.management.commands._workspace.finalize import run_finalize
from teatree.core.management.commands._workspace.forge_pr_state import read_live_pr_state
from teatree.core.management.commands._workspace.landscape import LandscapeReport, run_landscape
from teatree.core.management.commands._workspace.owner_stamps import backfill_owner_stamps
from teatree.core.management.commands._workspace.provision_parallel import (
    provision_worktree_subprocess,
    render_worktree_report,
    run_worktree_provisions_in_parallel,
)
from teatree.core.management.commands._workspace.relocate import RelocateIO, active_overlay_name, run_relocate
from teatree.core.management.commands._workspace.salvage import emit_records_json, run_salvage
from teatree.core.management.commands._workspace.stamp_identity import StampResult, run_stamp_identity
from teatree.core.management.commands._workspace.ticket_intake import (
    ForeignIssueWorktreeRefusedError,
    InvalidTicketKindError,
    RawTicketInputs,
    adopt_preflight_refusal,
    build_intake,
    build_ticket,
    finalize_ticket_provision,
    resolve_adopt_context,
)
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners import WorktreeStartRunner, WorktreeTeardownRunner
from teatree.core.worktree.branch_upstream import repair_clones
from teatree.core.worktree.branch_verdict import branch_verdict_report, render_verdict
from teatree.core.worktree.dead_row_release import release_dead_rows
from teatree.core.worktree.occupancy import WorktreeOccupiedError, refuse_if_ticket_checkout_occupied
from teatree.core.worktree.worktree_done import reap_done_worktrees
from teatree.docker.reclaim import reclaim_disk


def _worktree_root() -> Path:
    # The per-overlay WORKTREE root (env → DB ConfigSetting → default) where NEW
    # ticket worktrees land — NOT the CLONE root (``config.clone_root()``,
    # ``~/workspace``) where source clones are discovered.
    return _config_worktree_root()


class Command(TyperCommand):
    @command()
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def ticket(  # noqa: PLR0913 — django-typer command: every param maps 1:1 to a CLI flag; the arg list IS the public `workspace ticket` surface, not an internal design smell.
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
        adopt: Annotated[
            bool,
            typer.Option(
                "--adopt",
                help="Adopt the branch checked out in the current git worktree (auto-detect), "
                "registering Ticket + Worktree rows against it instead of deriving <number>-<slug> (#2275).",
            ),
        ] = False,
        adopt_branch: Annotated[
            str,
            typer.Option(
                "--adopt-branch",
                help="Adopt this EXISTING branch (implies --adopt). Omit to auto-detect from the current git worktree.",
            ),
        ] = "",
        adopt_closed: Annotated[
            bool,
            typer.Option(
                "--adopt-closed",
                help="Override the --adopt guard that refuses a CLOSED/nonexistent target issue/PR URL.",
            ),
        ] = False,
        kind: Annotated[
            str, typer.Option("--kind", help="Classify: 'fix' or 'feature' (blank infers from the title, #17).")
        ] = "",
    ) -> int:
        """Create or update a ticket and trigger worktree provisioning."""
        _wh.warn_orphans(self.stderr.write)
        # #1310: a multi-overlay install with ``T3_OVERLAY_NAME`` missing
        # used to die on the ambiguous ``get_overlay()`` call here.
        # Infer from the issue URL whose workspace repos own it; the
        # default ``get_overlay()`` env-var path still wins when set.
        overlay = get_overlay(_wh.resolve_overlay_name_for_url(issue_url))
        # Refuse/canonicalize a non-URL arg (a bare ``3274``) BEFORE it can be
        # persisted as a malformed ``issue_url`` — resolve it to the overlay's
        # full issue URL or reject it.
        try:
            issue_url = canonicalize_issue_ref(overlay, issue_url)
        except InvalidIssueRefError as exc:
            self.stderr.write(f"  Refused: {exc}")
            return 0
        adopt_ctx = resolve_adopt_context(adopt=adopt, adopt_branch=adopt_branch)
        adopt_refusal = adopt_preflight_refusal(overlay, issue_url, adopt_ctx, allow_closed=adopt_closed)
        if adopt_refusal is not None:
            self.stderr.write(adopt_refusal)
            return 0
        raw = RawTicketInputs(issue_url, repos, variant, description, take_over, adopt=adopt_ctx, kind=kind)
        try:
            intake = build_intake(overlay, raw)
            ticket = build_ticket(self.stderr.write, overlay, intake, _worktree_root())
        except InvalidTicketKindError as exc:
            self.stderr.write(f"  Refused: {exc}")
            return 0
        except ForeignIssueWorktreeRefusedError:
            return 0

        # #3952: re-resolving a ticket whose checkout a live agent already holds
        # would hand a second actor into that working tree. ``--take-over`` is the
        # operator's explicit override, the same escape the #2217 foreign-worktree
        # refusal above already uses.
        if not take_over:
            try:
                refuse_if_ticket_checkout_occupied(ticket)
            except WorktreeOccupiedError as exc:
                self.stderr.write(f"  Refused: {exc}")
                self.stderr.write("  Pass --take-over to proceed anyway.")
                return 0

        return finalize_ticket_provision(
            self.stdout.write,
            self.stderr.write,
            ticket,
            adopt_ctx,
            _worktree_root(),
        )

    @command()
    def provision(
        self,
        ticket_id: int = typer.Argument(0, help="Optional ticket id (alias for PWD auto-detect; #941)."),
        path: str = typer.Option("", help="Worktree path inside the workspace (auto-detects from PWD)."),
        slow_import: bool = typer.Option(default=False, help="Allow slow DB fallbacks."),  # noqa: FBT001 — typer CLI boolean flag; the bool parameter is typer's option idiom
        report: bool = typer.Option(  # noqa: FBT001 — typer CLI boolean flag; the bool parameter is typer's option idiom
            default=False,
            help="Print each worktree's per-step provision-report table (total + slowest step).",
        ),
    ) -> int:
        """Provision every worktree in the current ticket workspace, in parallel."""
        ticket = Ticket.objects.filter(pk=ticket_id).first() if ticket_id else None
        if ticket is None:
            ticket = resolve_workspace_ticket(path)
        # #1310: disambiguate from ``ticket.overlay`` so multi-overlay
        # installs don't die on ambiguous ``get_overlay()`` when
        # ``T3_OVERLAY_NAME`` env var is missing (a real path when a
        # caller bypasses the CLI bridge or the env is lost).
        overlay_name = ticket.overlay
        get_overlay(overlay_name or None)  # fail fast on an unresolvable overlay before spawning subprocesses

        # #2207: free abandoned unowned stacks (age-guarded) before the heavy
        # provisioning work competes with them for host CPU/RAM.
        reap_stale_local_stacks(self.stdout.write)

        worktrees = list(Worktree.objects.for_ticket(ticket))
        to_provision = [wt for wt in worktrees if wt.state in {Worktree.State.CREATED, Worktree.State.PROVISIONED}]
        results = run_worktree_provisions_in_parallel(
            to_provision,
            executor=lambda wt: provision_worktree_subprocess(wt, overlay_name=overlay_name, slow_import=slow_import),
            write=self.stdout.write,
        )
        if report:
            for wt in to_provision:
                wt.refresh_from_db()
                self.stdout.write(render_worktree_report(wt))

        failures = [r for r in results if not r.ok]
        if failures:
            names = ", ".join(f"{r.repo_path} ({r.detail})" for r in failures)
            _die(self.stderr.write, f"  Stopped: {names} — fix and re-run.")
        return len(worktrees)

    @command()
    def start(
        self,
        path: str = typer.Option("", help="Worktree path inside the workspace (auto-detects from PWD)."),
    ) -> str:
        """Start docker for every worktree in the current ticket workspace."""
        ticket = resolve_workspace_ticket(path)
        # #1310: disambiguate from ``ticket.overlay`` (see ``provision``).
        overlay = get_overlay(ticket.overlay or None)

        worktrees = list(Worktree.objects.for_ticket(ticket))
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
            commands = list(overlay.runtime.run_commands(wt))
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

        total, total_failures = _wh.report_worktree_probes(started, overlay, self.stdout.write, note_empty=False)
        if total_failures:
            _die(self.stderr.write, f"  {total_failures} of {total} probe(s) failed")
        return f"started {len(worktrees)} worktree(s)"

    @command()
    def ready(
        self,
        path: str = typer.Option("", help="Worktree path inside the workspace (auto-detects from PWD)."),
    ) -> str:
        """Run readiness probes for every worktree in the ticket workspace."""
        ticket = resolve_workspace_ticket(path)
        # #1310: disambiguate from ``ticket.overlay`` (see ``provision``).
        overlay = get_overlay(ticket.overlay or None)

        worktrees = list(Worktree.objects.for_ticket(ticket))
        total, total_failures = _wh.report_worktree_probes(worktrees, overlay, self.stdout.write, note_empty=True)
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
        allow_open_prs: bool = typer.Option(
            default=False,
            help="Reclaim the workspace even while one of the ticket's PRs/MRs is still open.",
        ),
    ) -> str:
        """Tear down every worktree in the current ticket workspace."""
        ticket = resolve_workspace_ticket(path)

        worktrees = list(Worktree.objects.for_ticket(ticket))
        check_no_open_prs(ticket, worktrees, read_pr_state=read_live_pr_state, allow_open_prs=allow_open_prs)
        labels: list[str] = []
        failures: list[str] = []
        for wt in worktrees:
            repo = wt.repo_path
            # teardown() keeps db_name/extra on the row (recovery pointers), so the
            # runner reads them straight off the live row — no snapshot to capture.
            with transaction.atomic():
                wt.teardown()
                wt.save()
            result = WorktreeTeardownRunner(wt, force=force).run()
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
        """Tear down every done worktree (analyze-then-wipe) on demand."""
        return reap_done_worktrees(_worktree_root(), dry_run=False)

    @command()
    def doctor(
        self,
        ticket: Annotated[int, typer.Option(help="Reconcile just this ticket pk; 0 = all tickets.")] = 0,
        *,
        fix: Annotated[bool, typer.Option(help="Apply fixes instead of just listing drift.")] = False,
    ) -> list[str]:
        """Detect state drift across every store; optionally fix it."""
        return run_drift_report(ticket_pk=ticket, fix=fix)

    @command(name="stamp-identity")
    def stamp_identity(self, repo: str = ".") -> StampResult:
        """Stamp the scoped noreply git identity onto an existing public GitHub clone (#762)."""
        return run_stamp_identity(repo)

    @command(name="list-orphans")
    def list_orphans(self) -> list[_wh.OrphanEntry]:
        """List orphan branches (commits ahead of origin/main AND no open PR) across the workspace."""
        return _wh.list_orphan_entries()

    @command()
    def landscape(self) -> LandscapeReport:
        """Survey what is already in flight or settled before planning (#2541)."""
        return run_landscape(_worktree_root())

    @command(name="branch-verdict")
    def branch_verdict(
        self,
        branches: Annotated[list[str], typer.Argument(help="Branch name(s) to judge — the sweep is one call.")],
        repo: Annotated[str, typer.Option(help="Repo/worktree path holding the branches.")] = ".",
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the verdicts as JSON on stdout.")] = False,
    ) -> None:
        """Is this branch's work already on the default branch? The canonical answer (#4070).

        Read-only. Serializes the three-layer content classifier, INCLUDING the forge
        signal beside the post-merge delta — a branch the forge calls merged whose tip
        still carries unique commits is reported NOT redundant, with those shas named, so
        "merged" is never readable on its own as "safe to delete".
        """
        verdicts = [branch_verdict_report(repo, branch) for branch in branches]
        self.print_result = False
        emit(
            verdicts,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(render_verdict(verdict) for verdict in verdicts),
        )

    @command(name="reap-stale")
    def reap_stale(
        self,
        min_age_minutes: int = typer.Option(
            0,
            help="Override the stale threshold (minutes); 0 uses the configured stale_stack_min_age_minutes.",
        ),
        dry_run: bool = typer.Option(default=False, help="List the stacks that would be reaped without removing."),  # noqa: FBT001 — CLI flag
    ) -> list[str]:
        """Tear down ABANDONED docker stacks no live worktree owns (age-guarded, #2207)."""
        return reap_stale_report(min_age_minutes=min_age_minutes, dry_run=dry_run, write_out=self.stdout.write)

    @command(name="reclaim-disk")
    def reclaim_disk_cmd(
        self,
        dry_run: bool = typer.Option(default=False, help="Plan the reclaim set without removing anything."),  # noqa: FBT001 — CLI flag
    ) -> str:
        """Free disk via the three safe Docker prunes, then STOP — engine: ``teatree.docker.reclaim`` (#2246)."""
        report = reclaim_disk(dry_run=dry_run)
        self.stdout.write(report.render())
        if report.failures:
            self.stderr.write(report.failure_summary())
            raise SystemExit(1)
        return ""

    @command(name="stamp-owners")
    def stamp_owners(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the stamping report as JSON on stdout instead of the human view."),
        ] = False,
    ) -> None:
        """Record which checkout owns each auto-isolated env dir THIS venue can see (#3872)."""
        report = backfill_owner_stamps(_worktree_root())
        self.print_result = False
        emit(
            report,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(report),
        )

    @command(name="clean-all")
    def clean_all(
        self,
        keep_dslr: int = typer.Option(1, help="Number of DSLR snapshots to keep per tenant."),
        *,
        dry_run: bool = typer.Option(
            default=False,
            help="Preview only: every pass reports what it WOULD do — each worktree that "
            "would WIPE (with its done-signal source) or be KEPT, plus the branch, stash, "
            "orphan DB/docker/env-root, raw-worktree and DSLR candidates — removing nothing.",
        ),
    ) -> list[str]:
        """Reap every done+redundant worktree, then prune branches/stashes, orphan DBs/docker/env-roots, DSLR."""
        return run_clean_all(
            _worktree_root(),
            CleanAllIO(write_out=self.stdout.write, write_err=self.stderr.write),
            keep_dslr=keep_dslr,
            dry_run=dry_run,
        )

    @command(name="release-dead-rows")
    def release_dead_rows_cmd(
        self,
        *,
        apply: bool = typer.Option(default=False, help="Actually release the rows. Without it, this is a dry run."),
        json_output: Annotated[bool, typer.Option("--json", help="Per-row dispositions as JSON.")] = False,
    ) -> None:
        """Release registered rows whose checkout is provably dead — ROWS ONLY (dry run unless --apply)."""
        outcome = release_dead_rows(_worktree_root(), dry_run=not apply)
        self.print_result = False
        emit(
            build_dead_row_report(outcome),
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: write_dead_row_lines(outcome, stream),
        )

    @command(name="repair-branch-upstreams")
    def repair_branch_upstreams(
        self,
        *,
        dry_run: bool = typer.Option(default=False, help="List the repairs without writing any git config."),
        json_output: Annotated[bool, typer.Option("--json", help="Per-branch outcomes as JSON.")] = False,
    ) -> None:
        """Point every branch tracking someone else's ref back at its own, or untrack it (#4225)."""
        outcomes = repair_clones(dry_run=dry_run) or ["No mistracked branch upstreams."]
        self.print_result = False
        emit(
            outcomes,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(outcomes) + "\n",
        )

    @command()
    def relocate(
        self,
        dry_run: bool = typer.Option(default=False, help="List the moves without moving anything."),  # noqa: FBT001 — CLI flag
    ) -> list[str]:
        """Move this overlay's teatree-managed worktrees under the per-overlay dir (regroup)."""
        io = RelocateIO(write_out=self.stdout.write, write_err=self.stderr.write)
        return run_relocate(active_overlay_name(), _config_worktree_root(), io, dry_run=dry_run).render()

    @command(name="emit")
    def emit(self) -> str:
        """Print the machine-readable JSON handoff for every NOT-auto-deleted item (#2763)."""
        # Return the JSON string only — django-typer serializes the return onto
        # stdout exactly once. A manual ``self.stdout.write(rendered)`` here (the
        # pre-PR-30 double-emit, #2763) printed it a SECOND time, so `json.loads`
        # failed with "Extra data" at the midpoint of the machine handoff.
        return emit_records_json(_worktree_root())

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
        """Capture a branch's unique content to a PR, verify it landed, then delete the branch (#2763)."""
        line = run_salvage(source_ref, salvage_branch=salvage_branch, target=target, allow_banned=allow_banned)
        # Emit the human outcome ONCE: `print_result = False` stops django-typer
        # repr'ing the return a second time (#2763's `workspace emit` double-emit).
        self.print_result = False
        self.stdout.write(line)
        return line

    @command(name="restore")
    def restore(
        self,
        reference: str,
        *,
        into: str = typer.Option("", help="Checkout to apply the bundle into (required — never inferred)."),
        dry_run: bool = typer.Option(default=False, help="Report whether each part applies; write nothing."),
    ) -> str:
        """Apply a captured salvage bundle back into a checkout (#4435)."""
        if not into.strip():
            _die(self.stderr.write, "workspace restore needs --into <checkout>: nothing is restored unnamed.\n")
        outcome = restore_bundle(reference, Path(into).expanduser(), dry_run=dry_run)
        self.print_result = False
        self.stdout.write(outcome.render())
        if not outcome.ok:
            raise SystemExit(1)
        return outcome.render()

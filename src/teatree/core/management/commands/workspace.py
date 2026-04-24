"""Workspace management: create ticket worktrees, finalize, clean stale branches."""

import os
import re
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.config import load_config
from teatree.core.cleanup import cleanup_worktree
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.core.reconcile import Drift, reconcile_all, reconcile_ticket
from teatree.core.runners import WorktreeProvisioner
from teatree.core.worktree_env import write_env_cache
from teatree.utils import git
from teatree.utils.db import drop_db
from teatree.utils.run import CommandFailedError, run_allowed_to_fail, run_checked

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra


def _worktree_map(repo: str) -> dict[str, str]:
    """Return ``{branch_name: worktree_path}`` for active git worktrees."""
    raw = git.run(repo=repo, args=["worktree", "list", "--porcelain"])
    result: dict[str, str] = {}
    current_path = ""
    for line in raw.splitlines():
        if line.startswith("worktree "):
            current_path = line.removeprefix("worktree ")
        elif line.startswith("branch refs/heads/"):
            result[line.removeprefix("branch refs/heads/")] = current_path
    return result


def _worktree_branches(repo: str) -> set[str]:
    """Return branch names linked to active git worktrees (safe to skip)."""
    return set(_worktree_map(repo))


def _is_squash_merged(repo: str, branch: str, default: str) -> bool:
    # GitHub: ask if a PR for this branch was merged.
    result = run_allowed_to_fail(
        ["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number", "--limit", "1"],
        cwd=repo,
        expected_codes=None,
    )
    if result.returncode == 0 and result.stdout.strip() not in {"", "[]"}:
        return True

    # GitLab: glab mr list output lines for found MRs start with "!" (e.g. "!5  Title  (branch)").
    result = run_allowed_to_fail(
        ["glab", "mr", "list", "--merged", "--source-branch", branch, "--limit", "1"],
        cwd=repo,
        expected_codes=None,
    )
    if result.returncode == 0 and any(line.lstrip().startswith("!") for line in result.stdout.splitlines()):
        return True

    # Fallback: empty diff means all changes are already in main.
    diff = git.run(repo=repo, args=["diff", f"origin/{default}...{branch}", "--stat"])
    return not diff


def _prune_squash_merged(repo: str, name: str, wt_map: dict[str, str]) -> str:
    """Remove a confirmed squash-merged branch (and its worktree if linked).

    Returns a status message — either a SKIPPED notice when unsynced commits
    are present or a confirmation that the branch was pruned.
    """
    unsynced = git.unsynced_commits(repo, name)
    if unsynced:
        return f"SKIPPED '{name}': {len(unsynced)} unsynced commit(s) — push to a new branch:\n  " + "\n  ".join(
            unsynced
        )
    wt_path = wt_map.get(name, "")
    if wt_path:
        git.worktree_remove(repo, wt_path)
        git.run(repo=repo, args=["worktree", "prune"])
    git.branch_delete(repo, name)
    return f"Pruned squash-merged branch: {name}"


def _prune_branches(repo: str) -> list[str]:
    """Delete local branches that are gone or merged.

    Handles squash-merged branches by comparing tree content against main.
    Prunes stale git worktree entries before checking branches so that
    worktrees whose directories no longer exist don't block cleanup.
    """
    cleaned: list[str] = []
    git.run(repo=repo, args=["fetch", "--prune"])
    git.run(repo=repo, args=["worktree", "prune"])

    current = git.current_branch(repo)
    default = git.default_branch(repo)
    protected = {current, default, "main", "master"}
    wt_branches = _worktree_branches(repo)

    wt_map = _worktree_map(repo)

    # Pass 1: delete "gone" branches that are not worktree-linked.
    for line in git.run(repo=repo, args=["branch", "-v", "--no-color"]).splitlines():
        if "[gone]" not in line:
            continue
        name = line.strip().removeprefix("+ ").split()[0]
        if name in protected or name in wt_branches:
            continue
        git.branch_delete(repo, name)
        cleaned.append(f"Pruned gone branch: {name}")

    # Pass 2: delete branches merged via regular merge.
    for line in git.run(repo=repo, args=["branch", "--merged", f"origin/{default}", "--no-color"]).splitlines():
        name = line.strip().removeprefix("* ").removeprefix("+ ")
        if name in protected or name in wt_branches:
            continue
        git.branch_delete(repo, name)
        cleaned.append(f"Pruned merged branch: {name}")

    # Pass 3: detect squash-merged branches (worktree-linked or not).
    # Squash-merge rewrites history so --merged can't detect them.
    # Uses the GitHub API as the primary signal, falls back to diff comparison.
    all_branches = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    }
    for name in sorted(all_branches - protected):
        if not _is_squash_merged(repo, name, default):
            continue
        cleaned.append(_prune_squash_merged(repo, name, wt_map))

    # Pass 4: warn about remaining non-protected branches with no merged PR.
    # Re-read after deletions above.
    remaining = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    } - protected
    for name in sorted(remaining):
        commits = git.run(repo=repo, args=["rev-list", "--count", f"{default}..{name}"])
        cleaned.append(f"WARNING: branch '{name}' has {commits} unpushed commit(s) and no merged PR")

    return cleaned


def _drop_orphaned_stashes(repo: str) -> list[str]:
    """Drop stashes whose branch no longer exists."""
    stash_list = git.run(repo=repo, args=["stash", "list"])
    if not stash_list:
        return []

    existing = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    }

    cleaned: list[str] = []
    entries = stash_list.splitlines()
    for i in range(len(entries) - 1, -1, -1):
        line = entries[i]
        if " on " not in line:
            continue
        branch_part = line.split(" on ", 1)[1].split(":")[0].strip()
        if branch_part not in existing:
            git.run(repo=repo, args=["stash", "drop", f"stash@{{{i}}}"])
            cleaned.append(f"Dropped orphaned stash: {line.split(':')[0]} (was on {branch_part})")

    return cleaned


def _drop_orphan_databases() -> list[str]:
    """Drop Postgres databases matching wt_* that don't belong to any worktree."""
    from teatree.utils.db import pg_env, pg_host, pg_user  # noqa: PLC0415

    result = run_allowed_to_fail(
        ["psql", "-h", pg_host(), "-U", pg_user(), "-l", "-t", "-A"],
        env=pg_env(),
        expected_codes=None,
    )
    if result.returncode != 0:
        return []

    all_dbs = {line.split("|")[0] for line in result.stdout.splitlines() if line}
    wt_dbs = {db for db in all_dbs if db.startswith("wt_")}

    known_db_names = set(Worktree.objects.exclude(db_name="").values_list("db_name", flat=True))

    orphans = wt_dbs - known_db_names
    cleaned: list[str] = []
    for db_name in sorted(orphans):
        run_allowed_to_fail(
            ["dropdb", "-h", pg_host(), "-U", pg_user(), "--if-exists", db_name],
            env=pg_env(),
            expected_codes=None,
        )
        cleaned.append(f"Dropped orphan database: {db_name}")
    return cleaned


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
        f"missing DB {m.db_name} for wt#{m.worktree_pk} — run `t3 lifecycle setup` to re-provision"
        for m in drift.missing_dbs
    )

    return fixes


def _resolve_unsynced_worktree(worktree: Worktree, exc: RuntimeError, *, interactive: bool) -> str:
    """Decide what to do with a worktree whose branch has genuinely-unpushed work.

    In a TTY, prompt the user: push to remote, abandon (force-clean), or skip.
    Non-interactive contexts (CI, scripts) preserve the old behaviour of
    listing the skip and exiting so the user can investigate.
    """
    if not interactive:
        return f"Skipped: {exc}"

    prompt = (
        f"\n{worktree.repo_path} ({worktree.branch}) — genuinely unpushed work.\n"
        f"  {exc}\n"
        "  [P]ush to remote / [A]bandon (force delete) / [S]kip (default): "
    )
    try:
        choice = input(prompt).strip().lower()
    except EOFError:
        return f"Skipped: {exc}"

    if choice == "p":
        return _push_unsynced_branch(worktree)
    if choice == "a":
        return _abandon_unsynced_branch(worktree)
    return f"Skipped: {exc}"


def _push_unsynced_branch(worktree: Worktree) -> str:
    wt_path = (worktree.extra or {}).get("worktree_path", "")
    if not wt_path or not Path(wt_path).is_dir():
        return f"Push failed: {worktree.repo_path} ({worktree.branch}) — worktree path missing"
    result = run_allowed_to_fail(
        ["git", "-C", wt_path, "push", "-u", "origin", worktree.branch],
        expected_codes=None,
    )
    if result.returncode != 0:
        return f"Push failed: {worktree.repo_path} ({worktree.branch}) — {result.stderr.strip()}"
    overlay_name = worktree.ticket.overlay or "<overlay>"
    return (
        f"Pushed: {worktree.repo_path} ({worktree.branch}). "
        f"Run `t3 {overlay_name} pr create {worktree.ticket.pk}` to open an MR."
    )


def _abandon_unsynced_branch(worktree: Worktree) -> str:
    try:
        return cleanup_worktree(worktree, force=True)
    except Exception as exc:  # noqa: BLE001
        return f"Abandon failed: {worktree.repo_path} ({worktree.branch}) — {exc}"


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
        overlay = get_overlay()
        repo_names = [r.strip() for r in repos.split(",") if r.strip()] if repos else overlay.get_workspace_repos()

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
                cleaned.append(_resolve_unsynced_worktree(wt, exc, interactive=interactive))

        for entry in workspace.iterdir():
            if entry.is_dir() and not any(entry.iterdir()):
                with suppress(OSError):
                    entry.rmdir()
                    cleaned.append(f"Removed empty dir: {entry.name}")

        cleaned.extend(_drop_orphan_databases())

        repo_root = Path.cwd()
        if (repo_root / ".git").exists():
            cleaned.extend(_prune_branches(str(repo_root)))
            cleaned.extend(_drop_orphaned_stashes(str(repo_root)))

        # Prune old DSLR snapshots
        from teatree.utils.django_db import prune_dslr_snapshots  # noqa: PLC0415

        pruned = prune_dslr_snapshots(keep=keep_dslr)
        cleaned.extend(f"Pruned DSLR snapshot: {name}" for name in pruned)

        return cleaned

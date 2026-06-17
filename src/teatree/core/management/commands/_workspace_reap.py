"""Per-worktree resilient teardown for the ``workspace clean-all`` subcommand.

Its own module so :mod:`teatree.core.management.commands._workspace_cleanup`
stays under the module-health LOC + function caps. Owns the seam both
``clean-all`` worktree loops funnel through (:func:`reap_one_worktree`) plus the
unsynced-work resolution it delegates to (interactive push/abandon/skip).
"""

import logging
import sys
from pathlib import Path

from teatree.core.cleanup import cleanup_worktree
from teatree.core.models import Worktree
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)


def _is_interactive() -> bool:
    """Whether ``clean-all`` may prompt on stdin.

    True only when both stdin and stdout are confirmed TTYs. Any non-TTY
    context — a piped stdin, an autonomous loop tick, a daemonised worker
    whose stdin is closed (``isatty`` raises ``ValueError``) or absent
    (``sys.stdin is None``) — resolves to ``False`` so the caller takes the
    safe documented non-interactive path and never blocks reading stdin.

    Fails closed to non-interactive: an unknown/unreadable stdin is treated
    as not-a-TTY, never as a TTY that may be prompted.
    """
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (ValueError, AttributeError):
        return False


def reap_one_worktree(worktree: Worktree, *, interactive: bool, strict_hygiene: bool = True) -> str:
    """Tear down one worktree row, translating every failure into a result line.

    The single resilient seam both ``clean-all`` worktree loops funnel through.
    :func:`cleanup_worktree` deletes the git worktree, ticket/env dir, SQLite DB
    and per-worktree docker for one row; this wrapper turns its failure modes
    into a kept-row warning string rather than letting any of them abort the
    whole ``clean-all`` run.

    ``strict_hygiene`` is forwarded to :func:`cleanup_worktree`. The squash-merged
    reaper passes ``False`` because :func:`is_squash_merged` has already confirmed
    the branch shipped — the origin/main-relative hygiene gate would otherwise
    re-refuse a genuinely squash-merged branch whose new SHA is ahead of
    origin/main (the squash subject differs, so the tree heuristic can be
    inconclusive without a forge PR). The always-on #706 data-loss guard
    (``_raise_if_unpushed``) still protects commits that exist on no remote, so a
    branch never pushed anywhere is still kept. The CREATED-state loop passes the
    default ``True`` (no prior merge confirmation).

    ``clean-all`` is always the caller, so ``keep_if_dirty=True`` is forwarded:
    a worktree with uncommitted changes (an agent mid-task) is KEPT, never
    bundle-and-reaped on a merged signal (#2243) — the data-loss guard the
    constraint requires for a live worktree.

    One failure mode is caught here. A ``RuntimeError`` is the #706/#835/#1506/#2243
    data-loss guards refusing genuinely-unsynced or uncommitted work; it is
    routed to :func:`resolve_unsynced_worktree`, which keeps the row and reports
    it (or, interactively, offers push/abandon).

    A row whose ``overlay`` is no longer registered — a foreign/unregistered
    overlay, or a sibling-repo worktree whose overlay was uninstalled — is no
    longer skipped (the under-reaping that left hundreds of stale worktrees +
    their docker/DB behind). :func:`cleanup_worktree` resolves the overlay
    tolerantly and runs the overlay-agnostic teardown so the row is actually
    reaped, with the same data-loss guards in force.
    """
    try:
        return str(cleanup_worktree(worktree, strict_hygiene=strict_hygiene, keep_if_dirty=True))
    except RuntimeError as exc:
        return resolve_unsynced_worktree(worktree, exc, interactive=interactive)


def resolve_unsynced_worktree(worktree: Worktree, exc: RuntimeError, *, interactive: bool) -> str:
    """Decide what to do with a worktree whose branch has genuinely-unpushed work."""
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
        return push_unsynced_branch(worktree)
    if choice == "a":
        return abandon_unsynced_branch(worktree)
    return f"Skipped: {exc}"


def push_unsynced_branch(worktree: Worktree) -> str:
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
        f"Run `t3 {overlay_name} pr create {worktree.ticket.pk}` to open a PR."
    )


def abandon_unsynced_branch(worktree: Worktree) -> str:
    try:
        return str(cleanup_worktree(worktree, force=True))
    except Exception as exc:  # noqa: BLE001
        return f"Abandon failed: {worktree.repo_path} ({worktree.branch}) — {exc}"

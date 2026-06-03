"""Orphan-branch PR creation with the #792 pre-push-deadlock deferral.

Split out of ``pr.py`` (same sibling-module pattern as ``_ship_fsm``) so
``pr.py`` stays within the module-health LOC budget and the "create the PR
for an orphan branch, or defer when the remote ref is not yet current"
concern is named by its own file.

#792: ``ensure-pr`` runs inside the git PRE-push hook. When the remote
branch ref already exists at an older base (``classify_branch`` ⇒
PUSHED_ORPHAN, not UNPUSHED_ORPHAN), ``gh pr create`` fails "No commits
between main and <branch>" because THIS push has not updated the remote
yet. Hard-failing there aborts the very push that would make the PR
creatable — a permanent deadlock. That specific failure is therefore
deferred (skip, exit 0) exactly like the documented first-push
UNPUSHED_ORPHAN case; the post-push ``ensure-pr`` opens the PR against the
now-current ref. Any other create failure is a real error and surfaces.
"""

from typing import TypedDict

from teatree.backends.protocols import PullRequestSpec
from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.ship import overlay_pr_labels, sanitize_close_keywords, should_close_ticket
from teatree.utils import git
from teatree.utils.run import CommandFailedError


class EnsurePrResult(TypedDict, total=False):
    skipped: str
    branch: str
    url: str
    hint: str
    error: str


def _ticket_extra_for_branch(branch_name: str) -> dict | None:
    """Return the owning ticket's ``extra`` for an orphan-branch PR, if any.

    The orphan path runs inside the git pre-push hook with no ticket
    handle. Resolving the ``extra`` via the branch's ``Worktree`` row lets
    ``should_close_ticket`` honor an explicit ``more_prs_coming`` opt-out
    even on this fallback. A genuinely orphan branch (no row) yields
    ``None`` — ``should_close_ticket`` then applies the close-on-merge
    default driven solely by the overlay setting.
    """
    from teatree.core.models import Worktree  # noqa: PLC0415 — avoid app-loading import cycle at module import

    worktree = Worktree.objects.filter(branch=branch_name).order_by("-id").first()
    if worktree is None:
        return None
    ticket = worktree.ticket
    return ticket.extra if isinstance(ticket.extra, dict) else None


def _branch_own_commit_message(repo_path: str, branch_name: str) -> tuple[str, str]:
    """Return ``(subject, body)`` of the branch's OWN first (oldest) commit.

    #1534: the PR title/body must describe the work being shipped — the
    branch's own commit — never the default branch's head. Reading
    ``HEAD`` (the former behaviour) could pick up an unrelated, already-
    merged commit when the wrong ref was checked out or ``--repo`` was a
    slug, opening a PR titled after a stale default-branch commit. Sourcing
    explicitly from ``origin/<default>..<branch>`` makes the title
    independent of the working tree and matches the squash-PR-title
    convention (the branch's first own commit). The oldest unique commit is
    the canonical title when the branch has several. No unique commit yields
    ``("", "")`` so the caller keeps its safe ``WIP:`` fallback rather than
    mislabelling the PR after the default-branch head.
    """
    try:
        default = git.default_branch(repo=repo_path)
    except (CommandFailedError, RuntimeError, ValueError):
        default = "main"
    return git.first_commit_message(repo=repo_path, range_spec=f"origin/{default}..{branch_name}")


def create_or_defer_pr(repo_path: str, branch_name: str) -> EnsurePrResult:
    """Build the PR spec from the branch's own commit and create it, or defer (#792).

    The "no commits between" create failure is the pre-push stale-remote
    race (the remote ref still lags this in-flight push); deferring it lets
    the push proceed so the post-push ``ensure-pr`` opens the PR. Every
    other create failure is real and re-raised.
    """
    host = code_host_from_overlay()
    if host is None:
        return EnsurePrResult(error="no code host configured")

    commit_subject, commit_body = _branch_own_commit_message(repo_path, branch_name)
    title = commit_subject or f"WIP: {branch_name}"
    raw_description = (
        f"{commit_subject}\n\n{commit_body}"
        if commit_subject and commit_body
        else (commit_subject or commit_body or f"PR auto-created to track branch `{branch_name}`.")
    )
    close_ticket = should_close_ticket(
        _ticket_extra_for_branch(branch_name),
        setting_enabled=get_overlay().config.mr_close_ticket,
    )
    description = sanitize_close_keywords(raw_description, close_ticket=close_ticket)

    remote = git.remote_url(repo=repo_path)
    repo_slug = git.slug_from_remote(remote)
    assignee = host.current_user() or git.config_value(key="user.name")

    try:
        raw = host.create_pr(
            PullRequestSpec(
                repo=repo_slug,
                branch=branch_name,
                title=title,
                description=description,
                labels=overlay_pr_labels(),
                assignee=assignee,
                draft=False,
            ),
        )
    except CommandFailedError as exc:
        if "no commits between" in (exc.stderr or str(exc)).lower():
            return EnsurePrResult(
                skipped="remote ref not yet current (pre-push race) — re-run after push completes",
                branch=branch_name,
                hint=f"t3 <overlay> pr ensure-pr --branch {branch_name}",
            )
        raise
    # #1222 / #1226: ``web_url`` is the cross-host canonical key (GitLab
    # API native; GitHub backend was aligned to it). ``html_url`` is kept
    # for raw GitHub API payloads piped through other producers. An empty
    # / non-URL payload surfaces as ``error`` so the orphan-branch path
    # never silently advances with no PR — same invariant the ship runner
    # enforces.
    url = str(raw.get("web_url") or raw.get("html_url") or "")
    if not url.startswith(("http://", "https://")):
        return EnsurePrResult(
            branch=branch_name,
            error=f"host.create_pr returned no PR url (got {url!r}; payload keys={sorted(raw.keys())!r})",
        )
    return EnsurePrResult(branch=branch_name, url=url)

"""Orphan-branch PR creation with the #792 pre-push-deadlock deferral.

Split out of ``pr.py`` (same sibling-module pattern as ``_ship.fsm``) so
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

from typing import TYPE_CHECKING, TypedDict

from teatree.core.backend_factory import code_host_for_repo_from_overlay
from teatree.core.backend_protocols import BackendResolutionError, CodeHostBackend, PullRequestSpec
from teatree.core.gates.architecture_precheck_gate import warn_if_precheck_incomplete
from teatree.core.gates.debt_delta_gate import evaluate_debt_delta
from teatree.core.gates.open_questions_gate import warn_if_open_questions_missing
from teatree.core.gates.pr_budget_gate import PrBudgetExceededError, check_pr_budget
from teatree.core.merge.pr_assignee import resolve_pr_assignee
from teatree.core.merge.pr_create_verify import verify_pr_exists
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.ship import overlay_pr_labels, sanitize_close_keywords, should_close_ticket
from teatree.utils import git, git_remote
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.models import Ticket
    from teatree.types import RawAPIDict


class EnsurePrResult(TypedDict, total=False):
    skipped: str
    branch: str
    url: str
    hint: str
    error: str


def _ticket_for_branch(branch_name: str) -> "Ticket | None":
    """Return the owning ``Ticket`` for an orphan-branch PR, via the branch's ``Worktree`` row.

    The orphan path runs inside the git pre-push hook with no ticket handle;
    the most-recent ``Worktree`` on *branch_name* names its ticket. A genuinely
    orphan branch (no row) yields ``None``.
    """
    from teatree.core.models import Worktree  # noqa: PLC0415 — avoid app-loading import cycle at module import

    worktree = Worktree.objects.filter(branch=branch_name).order_by("-id").first()
    return worktree.ticket if worktree is not None else None


def _ticket_extra_for_branch(branch_name: str) -> dict | None:
    """Return the owning ticket's ``extra`` for an orphan-branch PR, if any.

    Resolving the ``extra`` via the branch's ``Worktree`` row lets
    ``should_close_ticket`` honor an explicit ``more_prs_coming`` opt-out
    even on this fallback. A genuinely orphan branch (no row) yields
    ``None`` — ``should_close_ticket`` then applies the close-on-merge
    default driven solely by the overlay setting.
    """
    ticket = _ticket_for_branch(branch_name)
    if ticket is None:
        return None
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
    # #2025: resolve the forge from the branch's repo origin host, not by
    # token-presence precedence — opening a PR on a GitLab-hosted repo with
    # a GitHub-first overlay ran ``gh`` against a GitLab remote.
    try:
        host = code_host_for_repo_from_overlay(repo_path)
    except BackendResolutionError as exc:
        return EnsurePrResult(error=str(exc))
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
    warn_if_open_questions_missing(description)
    warn_if_precheck_incomplete(description)

    remote = git.remote_url(repo=repo_path)
    repo_slug = git_remote.slug_from_remote(remote)
    assignee = resolve_pr_assignee(host, repo=repo_slug)

    # North-star PR-2/PR-3: refuse before opening when the ticket is already at its
    # per-repo open-PR budget, or when the branch introduces unwaived net-new tech
    # debt. Both inert at their DARK/neutral defaults; a genuinely orphan branch (no
    # owning ticket) has no budget/plan scope, so the checks are skipped.
    owning_ticket = _ticket_for_branch(branch_name)
    if owning_ticket is not None:
        try:
            check_pr_budget(owning_ticket, repo_slug)
        except PrBudgetExceededError as exc:
            return EnsurePrResult(branch=branch_name, error=str(exc))
        debt_error = evaluate_debt_delta(owning_ticket, repo_path)
        if debt_error is not None:
            return EnsurePrResult(branch=branch_name, error=debt_error)

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
    return _verified_pr_result(host, raw, branch_name)


def _verified_pr_result(host: CodeHostBackend, raw: "RawAPIDict", branch_name: str) -> EnsurePrResult:
    """Turn a ``create_pr`` payload into a result, verifying the URL is a live PR.

    #1222 / #1226: ``web_url`` is the cross-host canonical key (GitLab API
    native; GitHub backend was aligned to it); ``html_url`` is kept for raw
    GitHub payloads. An empty / non-URL payload surfaces as ``error`` so the
    orphan-branch path never silently advances with no PR. #1194: a well-formed
    URL is not proof — re-read it; a 404 means the create silently no-op'd.
    """
    url = str(raw.get("web_url") or raw.get("html_url") or "")
    if not url.startswith(("http://", "https://")):
        return EnsurePrResult(
            branch=branch_name,
            error=f"host.create_pr returned no PR url (got {url!r}; payload keys={sorted(raw.keys())!r})",
        )
    verified = verify_pr_exists(host, url)
    if not verified.confirmed:
        return EnsurePrResult(
            branch=branch_name,
            error=f"host.create_pr URL {url!r} failed verify-by-re-read: {verified.reason}",
        )
    return EnsurePrResult(branch=branch_name, url=url)

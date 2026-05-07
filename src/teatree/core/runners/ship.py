import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

from teatree.backends.protocols import PullRequestSpec
from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.utils import git

if TYPE_CHECKING:
    from teatree.backends.protocols import CodeHostBackend
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import TicketExtra

logger = logging.getLogger(__name__)

_CLOSE_KEYWORD_RE = re.compile(
    r"\b(closes?|fixes?|resolves?)\s+((?:[\w./-]+)?#\d+|https?://\S+/issues/\d+)",
    re.IGNORECASE,
)


def sanitize_close_keywords(description: str, *, close_ticket: bool) -> str:
    """Replace ``Closes/Fixes/Resolves #N`` with ``Relates to`` when not closing."""
    if close_ticket:
        return description
    return _CLOSE_KEYWORD_RE.sub(r"Relates to \2", description)


def overlay_mr_labels() -> list[str]:
    raw = get_overlay().config.mr_auto_labels
    if isinstance(raw, str):
        values: Iterable[str] = raw.split(",")
    elif isinstance(raw, Iterable):
        values = [str(value) for value in raw]
    else:
        return []
    return [value.strip() for value in values if value.strip()]


class ShipExecutor(RunnerBase):
    """Push the worktree branch and open the pull request.

    Runs inside ``execute_ship`` after the FSM advances to ``SHIPPED``. The
    worker calls ``request_review()`` on success to advance to ``IN_REVIEW``.
    """

    def __init__(self, ticket: "Ticket") -> None:
        self.ticket = ticket

    def run(self) -> RunnerResult:
        ticket = self.ticket
        extra = cast("TicketExtra", ticket.extra or {})
        existing_urls = list(extra.get("pr_urls") or [])
        if existing_urls:
            return RunnerResult(ok=True, detail=existing_urls[-1])

        worktree = ticket.worktrees.first()  # ty: ignore[unresolved-attribute]
        if worktree is None:
            return RunnerResult(ok=False, detail="no worktree on ticket")

        host = code_host_from_overlay()
        if host is None:
            return RunnerResult(ok=False, detail="no code host configured")

        repo_path = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
        branch = worktree.branch

        git.push(repo=repo_path, remote="origin", branch=branch)

        spec = self._build_pr_spec(ticket, host, repo_path, branch, extra)
        pr = host.create_pr(spec)
        url = str(pr.get("web_url") or pr.get("html_url") or "")
        self._record_pr_url(ticket, extra, url)
        logger.info("Ship executor pushed %s and opened PR %s", branch, url)
        return RunnerResult(ok=True, detail=url)

    @staticmethod
    def _build_pr_spec(
        ticket: "Ticket",
        host: "CodeHostBackend",
        repo_path: str,
        branch: str,
        extra: "TicketExtra",
    ) -> PullRequestSpec:
        title_override = str(extra.get("pr_title_override") or "")
        subject, body = git.last_commit_message(repo=repo_path)
        title = title_override or subject or f"Resolve {ticket.issue_url}"
        raw_description = f"{subject}\n\n{body}" if subject and body else (subject or body)
        description = sanitize_close_keywords(raw_description, close_ticket=get_overlay().config.mr_close_ticket)
        assignee = host.current_user() or git.config_value(key="user.name")
        return PullRequestSpec(
            repo=repo_path,
            branch=branch,
            title=title,
            description=description,
            labels=overlay_mr_labels(),
            assignee=assignee,
        )

    @staticmethod
    def _record_pr_url(ticket: "Ticket", extra: "TicketExtra", url: str) -> None:
        urls = list(extra.get("pr_urls") or [])
        if url and url not in urls:
            urls.append(url)
        extra["pr_urls"] = urls
        extra.pop("pr_title_override", None)
        ticket.extra = extra
        ticket.save(update_fields=["extra"])

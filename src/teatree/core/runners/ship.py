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
    """Push the worktree branch and open the merge request.

    Runs inside ``execute_ship`` after the FSM advances to ``SHIPPED``. The
    worker calls ``request_review()`` on success to advance to ``IN_REVIEW``.
    """

    def run(self) -> RunnerResult:
        ticket = self.ticket
        worktree = ticket.worktrees.first()  # ty: ignore[unresolved-attribute]
        if worktree is None:
            return RunnerResult(ok=False, detail="no worktree on ticket")

        host = code_host_from_overlay()
        if host is None:
            return RunnerResult(ok=False, detail="no code host configured")

        repo_path = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
        branch = worktree.branch

        git.push(repo=repo_path, remote="origin", branch=branch)

        subject, body = git.last_commit_message(repo=repo_path)
        title = subject or f"Resolve {ticket.issue_url}"
        description = sanitize_close_keywords(body, close_ticket=get_overlay().config.mr_close_ticket)
        assignee = host.current_user() or git.config_value(key="user.name")

        mr = host.create_pr(
            PullRequestSpec(
                repo=repo_path,
                branch=branch,
                title=title,
                description=description,
                labels=overlay_mr_labels(),
                assignee=assignee,
            ),
        )

        url = str(mr.get("web_url") or mr.get("html_url") or "")
        extra = cast("TicketExtra", ticket.extra or {})
        urls = list(extra.get("mr_urls") or [])
        if url and url not in urls:
            urls.append(url)
        extra["mr_urls"] = urls
        ticket.extra = extra
        ticket.save(update_fields=["extra"])

        logger.info("Ship executor pushed %s and opened MR %s", branch, url)
        return RunnerResult(ok=True, detail=url)

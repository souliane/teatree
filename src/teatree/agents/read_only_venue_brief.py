"""Facts a phase in the read-only executor receives inline: it reaches no forge, no data dir and no ``t3``."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from teatree.agents._runner_options import _resolve_task_cwd
from teatree.agents.context_budget import MAX_APPEND_BYTES
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
from teatree.agents.phase_blocks import intake_survey_json
from teatree.core.backend_registry import get_backend_provider
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.review_target import review_target_for_task
from teatree.core.overlay_loader import get_overlay_for_ticket
from teatree.core.review.head_workflow_runs import live_checks_at
from teatree.core.review.live_head import live_head_at
from teatree.core.worktree.target_branch import resolve_target_branch
from teatree.utils.git_sync import fetch_branch

if TYPE_CHECKING:
    from teatree.core.models import Task, Ticket

ISSUE_BODY_CAP_BYTES: Final = 64 * 1024
ISSUE_COMMENTS_CAP_BYTES: Final = 64 * 1024
SURVEY_CAP_BYTES: Final = 32 * 1024
REVIEW_DIFF_CAP_BYTES: Final = 128 * 1024
REVIEW_CHECKS_CAP_BYTES: Final = 4 * 1024
HANDOFF_CAP_BYTES: Final = 32 * 1024
TICKET_CONTEXT_CAP_BYTES: Final = 32 * 1024
ABSENT_TOOL: Final = "`<teatree tool, absent here>`"
_TEATREE_COMMAND: Final = re.compile(r"(?<![\w/:.-])t3 ")
_TEATREE_TOOL_SPAN: Final = re.compile(r"`[^`\n]*(?:(?<![\w/:.-])t3 |mcp__teatree__)[^`\n]*`")
_TEATREE_CODE_FENCE: Final = re.compile(r"^[ \t]*```[^\n]*\n.*?^[ \t]*```[ \t]*$", re.MULTILINE | re.DOTALL)

READ_ONLY_DIRECTIVE: Final = (
    "READ-ONLY VENUE: every checkout here is mounted read-only; only $TMPDIR is writable, and it is discarded.",
    "There is no teatree CLI, no forge credential, no teatree MCP server and no push from here. Do not run the",
    "teatree CLI, gh, git fetch, git push, git worktree add, uv sync or any test: whatever the skills below say to",
    "record or post through them, return in your final JSON envelope instead. Your facts are in the task message.",
)


@dataclass(frozen=True, slots=True)
class ReadOnlyFacts:
    issue_body: str
    issue_comments: str
    ticket_context: str
    handoff: str
    survey: str
    review_head: str
    review_diff: str
    review_checks: str

    @classmethod
    def gather(cls, task: "Task", handoff: Path | None) -> "ReadOnlyFacts":
        """Read everything the brief inlines in the worker, or refuse: nothing is truncated on this venue."""
        issue_body, issue_comments = _issue(task.ticket)
        review_head, review_diff, review_checks = _review_diff(task)
        facts = cls(
            issue_body=issue_body,
            issue_comments=issue_comments,
            ticket_context=task.ticket.context.strip(),
            handoff=handoff.read_text(encoding="utf-8") if handoff is not None else "",
            survey=intake_survey_json(task) if normalize_phase(task.phase) == "planning" else "",
            review_head=review_head,
            review_diff=review_diff,
            review_checks=review_checks,
        )
        if normalize_phase(task.phase) == "planning" and not facts.survey:
            reason = "no intake landscape survey is recorded for this ticket"
            raise _refused(reason)
        for name, text, cap in (
            ("issue body", facts.issue_body, ISSUE_BODY_CAP_BYTES),
            ("issue comments", facts.issue_comments, ISSUE_COMMENTS_CAP_BYTES),
            ("ticket context", facts.ticket_context, TICKET_CONTEXT_CAP_BYTES),
            ("handoff", facts.handoff, HANDOFF_CAP_BYTES),
            ("survey", facts.survey, SURVEY_CAP_BYTES),
            ("review diff", facts.review_diff, REVIEW_DIFF_CAP_BYTES),
            ("review checks", facts.review_checks, REVIEW_CHECKS_CAP_BYTES),
        ):
            if (size := len(text.encode())) > cap:
                reason = f"read-only brief over budget: {name} {size} bytes > {cap}"
                raise _refused(reason)
        _fetch_target(task)
        return facts


def read_only_system_context(context: str) -> str:
    """*context* with every teatree command or tool it names rewritten, or refused: none of them runs here."""
    rewritten_fences = _TEATREE_CODE_FENCE.sub(
        lambda fence: _TEATREE_COMMAND.sub(f"{ABSENT_TOOL} ", fence.group()), context
    )
    rewritten = _TEATREE_TOOL_SPAN.sub(ABSENT_TOOL, rewritten_fences)
    if command := _TEATREE_COMMAND.search(rewritten):
        reason = (
            f"the system context still names a teatree command: {rewritten[command.start() : command.end() + 40]!r}"
        )
        raise _refused(reason)
    if (size := len(rewritten.encode())) > MAX_APPEND_BYTES:
        reason = f"read-only brief over budget: system context {size} bytes > {MAX_APPEND_BYTES}"
        raise _refused(reason)
    return rewritten


def read_only_text(text: str) -> str:
    rewritten = _TEATREE_TOOL_SPAN.sub(ABSENT_TOOL, text)
    return _TEATREE_COMMAND.sub(f"{ABSENT_TOOL} ", rewritten)


def _refused(reason: str) -> HarnessFallbackError:
    return HarnessFallbackError(
        f"read-only executor brief refused: {reason}", kind=HarnessFallbackKind.ACCESS, side_effects_started=False
    )


def _issue(ticket: "Ticket") -> tuple[str, str]:
    """The issue body and its comments, where owner decisions often land."""
    if not ticket.issue_url:
        return "", ""
    host = None
    try:
        host = get_backend_provider().get_code_host_for_url(get_overlay_for_ticket(ticket), ticket.issue_url)
        issue = host.get_issue(ticket.issue_url) if host is not None else {}
    except Exception as exc:  # an unreadable body refuses the run; the next route candidate plans instead
        reason = f"the issue body could not be read ({type(exc).__name__})"
        raise _refused(reason) from exc
    body = next((issue.get(key) for key in ("description", "body") if key in issue), None)
    if not isinstance(body, str):
        reason = "the issue body could not be read"
        raise _refused(reason)
    try:
        comments = host.list_issue_comments(issue_url=ticket.issue_url) if host is not None else []
    except Exception as exc:  # same as the body: a plan must not miss a decision recorded in a comment
        reason = f"the issue comments could not be read ({type(exc).__name__})"
        raise _refused(reason) from exc
    return body, "\n\n".join(block for comment in comments if (block := _comment_block(comment)))


def _comment_block(comment: object) -> str:
    """A human comment with its author and time; ``""`` for a system note or anything without a body."""
    if not isinstance(comment, dict) or comment.get("system") or not isinstance(body := comment.get("body"), str):
        return ""
    author = comment.get("user") or comment.get("author")
    name = (author.get("login") or author.get("username")) if isinstance(author, dict) else None
    return f"--- comment by {name or 'unknown'} at {comment.get('created_at', '')} ---\n{body}"


def _review_diff(task: "Task") -> tuple[str, str, str]:
    if normalize_phase(task.phase) != "reviewing" or task.ticket.role != task.ticket.Role.REVIEWER:
        return "", "", ""
    target = review_target_for_task(task)
    if target is None or not target.head_sha:
        reason = "the review has no dispatched pull request head"
        raise _refused(reason)
    head = live_head_at(slug=target.slug, pr_id=target.pr_id, host_kind=target.host_kind)
    if head.unreadable or head.sha != target.head_sha:
        reason = "the dispatched pull request head is unreadable or moved"
        raise _refused(reason)
    try:
        host = get_backend_provider().get_code_host_for_url(get_overlay_for_ticket(task.ticket), task.ticket.issue_url)
        diffs = host.get_pr_file_diffs(repo=target.slug, pr_iid=target.pr_id) if host is not None else None
    except Exception as exc:
        reason = f"the review diff could not be read ({type(exc).__name__})"
        raise _refused(reason) from exc
    if (
        not isinstance(diffs, Mapping)
        or not diffs
        or any(
            not isinstance(path, str) or not path or not isinstance(patch, str) or not patch.strip()
            for path, patch in diffs.items()
        )
    ):
        reason = "the review diff is empty or has an unreadable file"
        raise _refused(reason)
    current = live_head_at(slug=target.slug, pr_id=target.pr_id, host_kind=target.host_kind)
    if current.unreadable or current.sha != target.head_sha:
        reason = "the pull request head moved while its diff was read"
        raise _refused(reason)
    checks = live_checks_at(slug=target.slug, head_sha=target.head_sha)
    status = checks.status if checks.status in {"green", "pending", "failed"} else "unreadable"
    return (
        target.head_sha,
        "\n".join(f"--- {path} ---\n{patch}" for path, patch in diffs.items()),
        f"{status} — {checks.detail}",
    )


def _fetch_target(task: "Task") -> None:
    """Refresh the target ref in the worker, since the executor's clone is read-only and offline to git."""
    if (checkout := _resolve_task_cwd(task)) is None:
        return
    target = resolve_target_branch(task.ticket, checkout)
    try:
        fetch_branch(checkout, target.partition("/")[2])
    except Exception as exc:  # a stale base_sha would ship a plan against the wrong head
        reason = f"git fetch of {target} failed ({type(exc).__name__})"
        raise _refused(reason) from exc

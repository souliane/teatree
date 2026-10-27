"""Post the review request a read-only executor run drafted, through the worker's ``review_request_post`` command.

The executor holds no chat credential, no teatree MCP server and no forge, so it returns a
``review_request`` draft and stops. The worker is the only poster: the pull request, its head
and the owning ticket come from the worker's own rows, never from the envelope, and the post
goes through the one command that owns the dedup claim, the recorded approval and the send-proxy.
"""

import io
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Final, NamedTuple

from teatree.agents.result_schema import AgentResultBlob
from teatree.core.machine_output import call_command_streamed, last_json_object
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import PullRequest, Task
from teatree.core.review.live_head import live_head_at
from teatree.utils.url_slug import pr_ref_from_url

NOT_SENT_PREFIX: Final = "review_request_not_sent: "
_LANDED_ACTIONS: Final = frozenset({"post", "draft", "suppress"})
_MAX_TITLE_CHARS: Final = 200

type ReviewRequestPoster = Callable[..., Mapping[str, object]]


class _Target(NamedTuple):
    mr_url: str
    head_sha: str
    title: str


def post_through_command(*, mr_url: str, title: str, ticket_id: str, head_sha: str) -> Mapping[str, object]:
    """Run ``review_request_post``; its printed JSON verdict is the result."""
    out = io.StringIO()
    with suppress(SystemExit):
        call_command_streamed(
            "review_request_post",
            *("--mr-url", mr_url, "--approver", "requesting_review", "--title", title),
            *("--ticket-id", ticket_id, "--head-sha", head_sha),
            stream=out,
        )
    return last_json_object(out.getvalue()) or {"action": "refused", "reason": "unreadable_result"}


def record_returned_review_request(
    task: Task, result: AgentResultBlob, *, phase: str, poster: ReviewRequestPoster = post_through_command
) -> str:
    """Post the drafted request for *task*'s own pull request; return a refusal, or ``""`` when it landed.

    A no-op on every other phase and on a result that returned no draft (the evidence gate owns that).
    """
    if normalize_phase(phase or task.phase) != "requesting_review" or "review_request" not in result:
        return ""
    target = _target(task, result["review_request"])
    if isinstance(target, str):
        return target
    verdict = poster(mr_url=target.mr_url, title=target.title, ticket_id=str(task.ticket_id), head_sha=target.head_sha)
    action = str(verdict.get("action") or "")
    if action in _LANDED_ACTIONS:
        return ""
    return f"{NOT_SENT_PREFIX}{verdict.get('reason') or action or 'no verdict'}"


def _target(task: Task, draft: object) -> _Target | str:
    if not isinstance(draft, Mapping):
        return f"{NOT_SENT_PREFIX}expected an object, got {type(draft).__name__}"
    if draft.get("ready") is not True:
        return f"{NOT_SENT_PREFIX}the executor judged the pull request not ready: {_reasons(draft)}"
    row = _own_open_pull_request(task, str(draft.get("pr_url") or "").strip())
    ref = pr_ref_from_url(row.url) if row is not None else None
    if row is None or ref is None:
        return f"{NOT_SENT_PREFIX}pr_url names no open pull request of this ticket"
    head = live_head_at(slug=ref.slug, pr_id=ref.pr_id, host_kind=ref.host_kind)
    if head.unreadable or not head.sha:
        return f"{NOT_SENT_PREFIX}the live head of {row.url} could not be read"
    return _Target(row.url, head.sha, _subject(draft.get("title")))


def _own_open_pull_request(task: Task, wanted: str) -> PullRequest | None:
    rows = list(PullRequest.objects.filter(ticket_id=task.ticket_id, state=PullRequest.State.OPEN).order_by("pk"))
    if not wanted:
        return rows[0] if len(rows) == 1 else None
    return next((row for row in rows if row.url == wanted), None)


def _subject(raw: object) -> str:
    return " ".join(raw.split())[:_MAX_TITLE_CHARS] if isinstance(raw, str) else ""


def _reasons(draft: Mapping[str, object]) -> str:
    reasons = draft.get("reasons")
    return "; ".join(str(reason) for reason in reasons) if isinstance(reasons, list) and reasons else "no reason given"

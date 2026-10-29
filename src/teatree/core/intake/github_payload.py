"""Total field extraction from a GitHub delivery: an unexpected shape yields empty fields, never an exception."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from teatree.types import RawAPIDict

_PULL_REQUEST_EVENTS = frozenset({"pull_request", "pull_request_review", "pull_request_review_comment"})
_CHECK_EVENTS = frozenset({"check_run", "check_suite"})
_TIMESTAMP_PATHS = {
    "pull_request_review": ("review", "submitted_at"),
    "pull_request_review_comment": ("comment", "updated_at"),
    "issue_comment": ("comment", "updated_at"),
    "commit_comment": ("comment", "updated_at"),
    "pull_request": ("pull_request", "updated_at"),
    "issues": ("issue", "updated_at"),
    "check_suite": ("check_suite", "updated_at"),
    "check_run": ("check_run", "completed_at"),
    "workflow_run": ("workflow_run", "updated_at"),
    "push": ("head_commit", "timestamp"),
}
HOOK_EVENTS = tuple(_TIMESTAMP_PATHS)
_EARLIEST_EVENT = datetime(1970, 1, 1, tzinfo=UTC)
_LATEST_EVENT = datetime(2100, 1, 1, tzinfo=UTC)
_MAX_REPOSITORY_ID = 2**63 - 1


@dataclass(frozen=True, slots=True)
class GitHubPayloadFields:
    actor: str
    channel_ref: str
    thread_ref: str
    body: str
    repo_key: str = ""
    action: str = ""
    occurred_at: datetime | None = None

    @classmethod
    def from_payload(cls, event: str, payload: RawAPIDict) -> Self:
        thread_ref, body = _thread_and_body(event, payload)
        return cls(
            actor=_actor(payload),
            channel_ref=_str(_dict(payload, "repository"), "full_name"),
            thread_ref=thread_ref,
            body=body,
            repo_key=_repo_key(payload),
            action=_str(payload, "action")[:64],
            occurred_at=_occurred_at(event, payload),
        )


def _repo_key(payload: RawAPIDict) -> str:
    repo_id = _dict(payload, "repository").get("id")
    if isinstance(repo_id, bool) or not isinstance(repo_id, int) or not 0 < repo_id <= _MAX_REPOSITORY_ID:
        return ""
    return f"github:{repo_id}"


def _occurred_at(event: str, payload: RawAPIDict) -> datetime | None:
    path = _TIMESTAMP_PATHS.get(event)
    if path is None:
        return None
    stamp = _str(_dict(payload, path[0]), path[1])
    if not stamp:
        return None
    try:
        parsed = parse_datetime(stamp)
        if parsed is None or not timezone.is_aware(parsed):
            return None
        occurred = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None
    return parsed if _EARLIEST_EVENT <= occurred < _LATEST_EVENT else None


def _thread_and_body(event: str, payload: RawAPIDict) -> tuple[str, str]:
    if event in _PULL_REQUEST_EVENTS:
        pull_request = _dict(payload, "pull_request")
        return _number(pull_request, "number"), _str(pull_request, "title")
    if event == "issue_comment":
        return _number(_dict(payload, "issue"), "number"), _str(_dict(payload, "comment"), "body")
    if event == "issues":
        issue = _dict(payload, "issue")
        return _number(issue, "number"), _str(issue, "title")
    if event == "push":
        return _str(payload, "ref"), _str(_dict(payload, "head_commit"), "message")
    if event in _CHECK_EVENTS:
        check = _dict(payload, event)
        name = _str(check, "name") or event
        return _number(check, "id"), f"{name}: {_str(check, 'conclusion') or _str(check, 'status')}"
    return "", _str(payload, "action")


def _actor(payload: RawAPIDict) -> str:
    return (
        _str(_dict(payload, "sender"), "login")
        or _str(_dict(_dict(payload, "review"), "user"), "login")
        or _str(_dict(payload, "pusher"), "name")
    )


def _dict(data: RawAPIDict, key: str) -> RawAPIDict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _str(data: RawAPIDict, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _number(data: RawAPIDict, key: str) -> str:
    value = data.get(key)
    return str(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else ""

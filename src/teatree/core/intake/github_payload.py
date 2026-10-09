"""Total field extraction from a GitHub delivery: an unexpected shape yields empty fields, never an exception."""

from dataclasses import dataclass
from typing import Self

from teatree.types import RawAPIDict

_PULL_REQUEST_EVENTS = frozenset({"pull_request", "pull_request_review", "pull_request_review_comment"})
_CHECK_EVENTS = frozenset({"check_run", "check_suite"})


@dataclass(frozen=True, slots=True)
class GitHubPayloadFields:
    actor: str
    channel_ref: str
    thread_ref: str
    body: str

    @classmethod
    def from_payload(cls, event: str, payload: RawAPIDict) -> Self:
        thread_ref, body = _thread_and_body(event, payload)
        return cls(
            actor=_actor(payload),
            channel_ref=_str(_dict(payload, "repository"), "full_name"),
            thread_ref=thread_ref,
            body=body,
        )


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

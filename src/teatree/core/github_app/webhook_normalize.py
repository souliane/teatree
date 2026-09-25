"""Normalize a GitHub event payload into an ``IngestionRecord`` (#4795).

The ONE normalizer :mod:`teatree.core.views.github_webhook` and the
``github_polling`` scanner both call: whichever transport observed the event,
the same payload shape produces the same record, so webhook and polling
"invoke the identical persistence/dispatch path" by construction rather than
by two normalizers kept in sync by convention.
"""

import hashlib
import json

from teatree.core.github_app.event_identity import identity_for
from teatree.core.views._webhook_persistence import IngestionRecord
from teatree.types import RawAPIDict


def normalize(event_type: str, payload: RawAPIDict, *, delivery_id: str = "") -> IngestionRecord:
    """Build the shared :class:`IngestionRecord` for *payload*.

    ``idempotency_key`` prefers the transport-independent identity
    (:func:`teatree.core.github_app.event_identity.identity_for`) so a webhook
    delivery and a poll discovery of the SAME logical update collapse onto one
    row; it falls back to *delivery_id* (the webhook's per-delivery UUID, absent
    for a poll-discovered record) and finally to a payload hash so ingestion
    never blocks on a family neither source can derive an identity for.
    """
    actor = _actor(payload)
    channel_ref = _str(_dict(payload, "repository"), "full_name")
    thread_ref, body = _thread_and_body(event_type, payload)
    identity = identity_for(event_type, payload)
    idempotency_key = identity or f"github:delivery:{delivery_id or _payload_hash(payload)}"
    return IngestionRecord(
        source="github",
        idempotency_key=idempotency_key,
        actor=actor,
        channel_ref=channel_ref,
        thread_ref=thread_ref,
        body=body,
        payload_json=dict(payload),
    )


def _thread_and_body(event_type: str, payload: RawAPIDict) -> tuple[str, str]:
    if event_type in {"pull_request", "pull_request_review", "pull_request_review_comment"}:
        pr = _dict(payload, "pull_request")
        return str(_int(pr, "number") or ""), _str(pr, "title")
    if event_type == "issue_comment":
        issue = _dict(payload, "issue")
        comment = _dict(payload, "comment")
        return str(_int(issue, "number") or ""), _str(comment, "body")
    if event_type == "issues":
        issue = _dict(payload, "issue")
        return str(_int(issue, "number") or ""), _str(issue, "title")
    if event_type == "push":
        commit = _dict(payload, "head_commit")
        return _str(payload, "ref"), _str(commit, "message")
    if event_type in {"check_run", "check_suite"}:
        check = _dict(payload, event_type)
        status = _str(check, "status")
        conclusion = _str(check, "conclusion")
        name = _str(check, "name") or event_type
        summary = f"{name}: {conclusion or status}".strip(": ")
        return str(_int(check, "id") or ""), summary
    return "", _str(payload, "action")


def _actor(payload: RawAPIDict) -> str:
    sender = _str(_dict(payload, "sender"), "login")
    if sender:
        return sender
    review_user = _str(_dict(_dict(payload, "review"), "user"), "login")
    if review_user:
        return review_user
    return _str(_dict(payload, "pusher"), "name")


def _payload_hash(payload: RawAPIDict) -> str:
    serialised = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(serialised).hexdigest()[:16]


def _dict(data: RawAPIDict, key: str) -> RawAPIDict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _str(data: RawAPIDict, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _int(data: RawAPIDict, key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["normalize"]

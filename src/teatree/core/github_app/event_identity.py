"""Canonical cross-transport identity for a GitHub event (#4795).

``IncomingEvent.idempotency_key`` collapses retries of the SAME delivery onto
one row; it does not, on its own, collapse a webhook delivery and a POLL
discovery of the SAME logical entity update, because the two transports carry
different envelopes (a delivery UUID vs. nothing at all). :func:`identity_for`
answers a narrower, transport-independent question — built from the entity's
OWN state (repo + entity kind + number/id + a change marker already present on
the entity) rather than either transport's envelope — so
:mod:`teatree.core.views.github_webhook` and the ``github_polling`` scanner
compute the SAME string for the same logical update and collapse onto one
``IncomingEvent`` (the validation/cutover window the ``polling``/``webhook``
presets both keep alive).

Returns ``None`` when the payload lacks the fields a family needs (an
unrecognised ``event_type``, or a shape too thin to derive a reliable change
marker from) — callers fall back to a transport-specific key so ingestion
still proceeds; it never raises on a malformed payload.
"""

from collections.abc import Callable

from teatree.types import RawAPIDict

_PULL_REQUEST_EVENTS = frozenset({"pull_request", "pull_request_review", "pull_request_review_comment"})

#: ``(entity_key, marker)`` — the identity split in two so a caller comparing
#: successive deliveries for the SAME entity (the webhook view's stale-replay
#: check) can compare markers directly instead of re-parsing a composed string
#: whose marker segment (an ISO-8601 timestamp) itself contains the delimiter.
EntityKeyAndMarker = tuple[str, str]


def identity_for(event_type: str, payload: RawAPIDict) -> str | None:
    """The canonical ``<entity_key>:<marker>`` identity for *payload*, or ``None``."""
    found = entity_key_and_marker(event_type, payload)
    return f"{found[0]}:{found[1]}" if found is not None else None


def entity_key_and_marker(event_type: str, payload: RawAPIDict) -> EntityKeyAndMarker | None:
    """The identity split as ``(entity_key, marker)``, or ``None`` when underivable.

    ``entity_key`` names the entity itself (family + repo + number/id) and is
    stable across updates; ``marker`` is what changed. Two deliveries for the
    same entity are comparable by marker alone when their ``entity_key``s match
    — see :mod:`teatree.core.views.github_webhook`'s stale-replay check.
    """
    repo = _str(_dict(payload, "repository"), "full_name")
    if not repo:
        return None
    family = event_type if event_type not in _PULL_REQUEST_EVENTS else "pull_request"
    extractor = _FAMILY_EXTRACTORS.get(family)
    return extractor(repo, payload) if extractor is not None else None


def _pull_request_identity(repo: str, payload: RawAPIDict) -> EntityKeyAndMarker | None:
    pr = _dict(payload, "pull_request")
    number = _int(pr, "number")
    updated_at = _str(pr, "updated_at")
    if not number or not updated_at:
        return None
    return f"github:pr:{repo}:{number}", updated_at


def _issue_identity(repo: str, payload: RawAPIDict) -> EntityKeyAndMarker | None:
    issue = _dict(payload, "issue")
    number = _int(issue, "number")
    updated_at = _str(issue, "updated_at")
    if not number or not updated_at:
        return None
    return f"github:issue:{repo}:{number}", updated_at


def _issue_comment_identity(repo: str, payload: RawAPIDict) -> EntityKeyAndMarker | None:
    comment = _dict(payload, "comment")
    comment_id = _int(comment, "id")
    updated_at = _str(comment, "updated_at")
    if not comment_id or not updated_at:
        return None
    return f"github:issue_comment:{repo}:{comment_id}", updated_at


def _push_identity(repo: str, payload: RawAPIDict) -> EntityKeyAndMarker | None:
    ref = _str(payload, "ref")
    after = _str(payload, "after")
    if not ref or not after:
        return None
    return f"github:push:{repo}:{ref}", after


def _check_run_identity(repo: str, payload: RawAPIDict) -> EntityKeyAndMarker | None:
    check_run = _dict(payload, "check_run")
    check_id = _int(check_run, "id")
    status = _str(check_run, "status")
    marker = _str(check_run, "completed_at") or _str(check_run, "started_at")
    if not check_id or not status or not marker:
        return None
    return f"github:check_run:{repo}:{check_id}", f"{status}:{marker}"


def _check_suite_identity(repo: str, payload: RawAPIDict) -> EntityKeyAndMarker | None:
    check_suite = _dict(payload, "check_suite")
    suite_id = _int(check_suite, "id")
    status = _str(check_suite, "status")
    marker = _str(check_suite, "updated_at")
    if not suite_id or not status or not marker:
        return None
    return f"github:check_suite:{repo}:{suite_id}", f"{status}:{marker}"


type _FamilyExtractor = Callable[[str, RawAPIDict], EntityKeyAndMarker | None]

#: One family name per GitHub event family this module knows how to derive an
#: identity for. ``identity_for`` collapses the three ``pull_request*`` webhook
#: event types onto the ``"pull_request"`` key before this lookup.
_FAMILY_EXTRACTORS: dict[str, _FamilyExtractor] = {
    "pull_request": _pull_request_identity,
    "issues": _issue_identity,
    "issue_comment": _issue_comment_identity,
    "push": _push_identity,
    "check_run": _check_run_identity,
    "check_suite": _check_suite_identity,
}


def _dict(data: RawAPIDict, key: str) -> RawAPIDict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _str(data: RawAPIDict, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _int(data: RawAPIDict, key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["EntityKeyAndMarker", "entity_key_and_marker", "identity_for"]

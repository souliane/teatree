"""Pattern-based intent classifier for ``IncomingEvent`` rows.

The classifier inspects the event's body and ``payload_json`` and emits an
``IntentClassification``. Rules are intentionally simple — the issue's
spec keeps an LLM fallback in reserve for ambiguous cases. The agent
loop's dispatcher branch consumes the classification's intent to route
the event to a task, question, approval, status, or escalation handler.

Reference: #654 § "Intent classifier".
"""

import re

from teatree.core.models import IncomingEvent, IntentClassification

_URGENT_PATTERN = re.compile(r"\b(urgent|asap|prod\s+(is\s+)?down|page(d)?\s+oncall|p[012])\b", re.IGNORECASE)
_QUESTION_PATTERN = re.compile(
    r"(\?|^|\s)(what|why|how|when|where|who|status\s+of|any\s+update|when\s+will)\b",
    re.IGNORECASE,
)
_TASK_PATTERN = re.compile(
    r"\b(can\s+you|could\s+you|please|implement|ship|deploy|add\s+|fix\s+|build\s+|write\s+|review)\b",
    re.IGNORECASE,
)
_APPROVAL_ACTIONS_GITLAB = {"approved"}
_APPROVAL_STATES_GITHUB = {"approved"}
_STATUS_OBJECT_KINDS = {"pipeline", "build", "deployment"}
_NOISE_SLACK_EVENT_TYPES = {"team_join", "channel_left", "channel_joined", "user_change", "presence_change"}

Verdict = tuple[str, float, str]


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def classify_event(event: IncomingEvent) -> IntentClassification:
    """Classify *event* and persist (or refresh) its ``IntentClassification``."""
    existing = IntentClassification.objects.filter(event=event).first()
    if existing is not None:
        return existing

    intent, confidence, rationale = _verdict_for(event)
    return IntentClassification.objects.create(
        event=event,
        intent=intent,
        confidence=confidence,
        rationale=rationale,
    )


def _verdict_for(event: IncomingEvent) -> Verdict:
    by_source = {
        IncomingEvent.Source.CI: _verdict_from_ci,
        IncomingEvent.Source.GITLAB: _verdict_from_gitlab,
        IncomingEvent.Source.GITHUB: _verdict_from_github,
        IncomingEvent.Source.SLACK: _verdict_from_slack,
    }
    source_verdict = by_source.get(event.source, lambda _e: None)(event)
    if source_verdict is not None:
        return source_verdict
    return _verdict_from_body(event.body or "")


def _verdict_from_ci(_event: IncomingEvent) -> Verdict:
    return IntentClassification.Intent.STATUS_UPDATE, 0.95, "ci event"


def _verdict_from_gitlab(event: IncomingEvent) -> Verdict | None:
    payload = event.payload_json or {}
    action = _as_dict(payload.get("object_attributes")).get("action")
    if action in _APPROVAL_ACTIONS_GITLAB:
        return IntentClassification.Intent.APPROVAL, 0.95, "gitlab mr approved"
    if (payload.get("object_kind") or "") in _STATUS_OBJECT_KINDS:
        return IntentClassification.Intent.STATUS_UPDATE, 0.9, "gitlab status event"
    return None


def _verdict_from_github(event: IncomingEvent) -> Verdict | None:
    payload = event.payload_json or {}
    review_state = _as_dict(payload.get("review")).get("state")
    if review_state in _APPROVAL_STATES_GITHUB:
        return IntentClassification.Intent.APPROVAL, 0.95, "github review approved"
    if payload.get("workflow_run") or payload.get("check_run"):
        return IntentClassification.Intent.STATUS_UPDATE, 0.9, "github status event"
    return None


def _verdict_from_slack(event: IncomingEvent) -> Verdict | None:
    payload = event.payload_json or {}
    slack_event_type = _as_dict(payload.get("event")).get("type") or ""
    if slack_event_type in _NOISE_SLACK_EVENT_TYPES or not (event.body or "").strip():
        return IntentClassification.Intent.NOISE, 0.95, "slack noise event"
    return None


def _verdict_from_body(body: str) -> Verdict:
    if _URGENT_PATTERN.search(body):
        return IntentClassification.Intent.ESCALATION, 0.9, "urgent keyword"
    if _TASK_PATTERN.search(body):
        return IntentClassification.Intent.TASK, 0.85, "imperative phrasing"
    if _QUESTION_PATTERN.search(body):
        return IntentClassification.Intent.QUESTION, 0.85, "question phrasing"
    return IntentClassification.Intent.NOISE, 0.5, "no rule matched"

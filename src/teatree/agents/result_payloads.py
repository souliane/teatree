"""What each envelope channel must carry for its recorder to PERSIST something."""

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from teatree.agents.result_schema import (
        AnswerEnvelope,
        ArticleSuggestion,
        DirectiveCandidateEnvelope,
        DirectiveInterpretationEnvelope,
        ReviewVerdictEnvelope,
        TriageRecommendation,
    )


def suggestion_url(item: object) -> str:
    """The persistable source URL of one article suggestion, or ``""`` if absent."""
    if not isinstance(item, dict):
        return ""
    return str(cast("ArticleSuggestion", item).get("url") or "").strip()


def answer_text(answer: object) -> str:
    """The persistable reply text of an answer envelope, or ``""`` if absent."""
    if not isinstance(answer, dict):
        return ""
    return str(cast("AnswerEnvelope", answer).get("text") or "").strip()


def recommendation_issue_url(item: object) -> str:
    """The persistable issue URL of one triage recommendation, or ``""`` if absent."""
    if not isinstance(item, dict):
        return ""
    return str(cast("TriageRecommendation", item).get("issue_url") or "").strip()


def recommendation_persists(item: object) -> bool:
    """Whether one triage recommendation carries what the recorder actually PERSISTS."""
    from teatree.core.models.pending_triage_recommendation import (  # noqa: PLC0415 — ORM/app-registry
        VALID_TRIAGE_VERDICTS,
    )

    if not recommendation_issue_url(item):
        return False
    verdict = str(cast("TriageRecommendation", item).get("verdict") or "").strip().lower()
    return verdict in VALID_TRIAGE_VERDICTS


def candidate_carries_payload(envelope: object) -> bool:
    """Whether a directive-candidate envelope carries something the recorder persists (#116)."""
    if not isinstance(envelope, dict):
        return False
    typed = cast("DirectiveCandidateEnvelope", envelope)
    return typed.get("is_directive") is True and bool(str(typed.get("normalized_constraint") or "").strip())


def interpretation_carries_payload(envelope: object) -> bool:
    """Whether a directive-interpretation envelope carries something the recorder persists."""
    if not isinstance(envelope, dict):
        return False
    typed = cast("DirectiveInterpretationEnvelope", envelope)
    sketch = typed.get("sketch")
    if isinstance(sketch, dict) and sketch:
        return True
    questions = typed.get("clarifying_questions")
    return isinstance(questions, list) and any(str(q).strip() for q in questions)


def verdict_carries_payload(envelope: object) -> bool:
    """Whether a review-verdict envelope names a verdict the recorder can persist (#3654)."""
    from teatree.core.models.review_verdict import ReviewVerdict  # noqa: PLC0415 — deferred: ORM/app-registry

    if not isinstance(envelope, dict):
        return False
    verdict = str(cast("ReviewVerdictEnvelope", envelope).get("verdict") or "").strip().lower()
    return verdict in {choice.value for choice in ReviewVerdict.Verdict}

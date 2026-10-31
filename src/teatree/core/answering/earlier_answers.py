"""Show the owner's recorded answer on this ticket before a repeat card is raised; code never answers for them."""

from datetime import timedelta

from django.utils import timezone

from teatree.core.modelkit.question_card import QuestionCard
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.question_text import question_fingerprint

EARLIER_ANSWER_WINDOW = timedelta(hours=24)
DIFFERS_PREFIX = "Differs from your earlier answer:"
MAX_SHOWN_ANSWERS = 3
MAX_SHOWN_CHARS = 120


def earlier_answer_problem(ticket_id: int | None, card: QuestionCard, question: str) -> str:
    """Why *card* must wait: the owner answered this kind here in the window and the card does not say how it differs.

    The same question asked again is no problem here: its own dedupe hands the answer back.
    """
    if ticket_id is None or card.why.strip().startswith(DIFFERS_PREFIX):
        return ""
    since = timezone.now() - EARLIER_ANSWER_WINDOW
    answers = list(DeferredQuestion.objects.owner_answers_on_ticket(ticket_id, card.decision, since=since))
    asked = question_fingerprint(question)
    if not answers or any(question_fingerprint(answer.question) == asked for answer in answers):
        return ""
    shown = "; ".join(
        f"{answer.question[:MAX_SHOWN_CHARS]!r} -> {answer.answer_text[:MAX_SHOWN_CHARS]!r}"
        for answer in answers[:MAX_SHOWN_ANSWERS]
    )
    return (
        f"the owner already answered this kind of question on this ticket in the last 24 hours ({shown}). "
        "If that answer settles yours, carry on with it. If it does not cover what you now ask "
        f"(a one-time approval covers only its own action), start the card's why with {DIFFERS_PREFIX!r} "
        "and say what is new"
    )

"""Shared test helper: answer a question the way the owner does, through the real Slack apply seam."""

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_binding import BoundAnswer, apply_bound_answer

OWNER_SLACK_ID = "U1"


def answer_on_slack(question: DeferredQuestion, text: str) -> None:
    assert apply_bound_answer(BoundAnswer(question=question, answer=text))

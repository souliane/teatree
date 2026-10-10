"""The owner-question message: the approved card layout as plain text and as Slack blocks (#4990).

One layout (:func:`~teatree.core.modelkit.question_card.layout`) feeds both, so the text a row was checked
as is the text it is sent as. A row recorded before cards existed is shown through the same layout; the
checks it must pass at send time are the ones every card passes at record time.
"""

import datetime as dt
import re
from dataclasses import dataclass, replace

from teatree.core.modelkit.question_card import CardOption, CardView, QuestionCard, layout, plain_text, shown_problems
from teatree.core.modelkit.reask_cadence import first_posted_at
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.types import RawAPIDict

_LEGACY_CONSEQUENCE = "I record this as your answer."
_ANSWER_CAP = 300
_HOURS_UNTIL_DAYS = 48
_SECONDS_PER_HOUR = 3600
_HOURS_PER_DAY = 24
WITHHELD_LINE = "I will ask this again in plain words."
ALREADY_ANSWERED = "Already answered: "
NO_LONGER_NEEDED = "This question no longer needs an answer."


@dataclass(frozen=True, slots=True)
class CardMessage:
    """What a Slack post or update carries: the fallback text and the blocks that show the same lines."""

    text: str
    blocks: list[RawAPIDict]


def _view(row: DeferredQuestion) -> CardView:
    card = QuestionCard.from_row(row.evidence, row.options_json)
    if card is not None:
        return card.view(row.question, ref=row.pk)
    options = tuple(
        replace(option, description=option.description or _LEGACY_CONSEQUENCE)
        for option in CardOption.parse_all(row.options_json)
    )
    return CardView(re.sub(r"\n{3,}", "\n\n", row.question.strip()), "", options, "", row.pk)


def _section(text: str) -> RawAPIDict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> RawAPIDict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _button(option: CardOption, index: int) -> RawAPIDict:
    button: RawAPIDict = {
        "type": "button",
        "text": {"type": "plain_text", "text": option.label},
        "action_id": f"owner-question-option-{index}",
        "value": str(index),
    }
    if option.recommended:
        button["style"] = "primary"
    return {"type": "actions", "elements": [button]}


def _head(view: CardView) -> list[RawAPIDict]:
    heading = f"*{view.question}*" + (f"\n{view.why}" if view.why else "")
    return [_section(heading), *([_section(f"> {view.quoted}")] if view.quoted else [])]


def render_text(row: DeferredQuestion) -> str:
    return plain_text(_view(row))


def render_blocks(row: DeferredQuestion) -> list[RawAPIDict]:
    """The card as Block Kit: one button per option, each followed by its consequence line."""
    view = _view(row)
    blocks = _head(view)
    for index, option in enumerate(view.options, 1):
        consequence = f"- {option.description}"
        blocks += [
            _button(option, index),
            _context(f"(recommended) {consequence}" if option.recommended else consequence),
        ]
    *_, [reply, ref] = layout(view)
    return [*blocks, _context(reply), _context(ref)]


def shown_problems_for(row: DeferredQuestion) -> list[str]:
    """Everything wrong with *row* as the owner would read it — the check every owner send passes."""
    return shown_problems(_view(row))


def answered_line(row: DeferredQuestion) -> str:
    """How an answered card closes: the option chosen with its consequence, or the owner's own words."""
    answer = row.answer_text.strip()
    for option in _view(row).options:
        if option.label == answer:
            return f"You chose: {option.label} - {option.description}" if option.description else f"You chose: {answer}"
    return f"You answered: {answer[:_ANSWER_CAP]}"


def closed_message(row: DeferredQuestion, line: str) -> CardMessage:
    """The card with its options and reply line replaced by *line*; the question, quote and ref stay."""
    view = _view(row)
    *_, [_, ref] = layout(view, closing=line)
    return CardMessage(plain_text(view, closing=line), [*_head(view), _section(line), _context(ref)])


def withheld_message() -> CardMessage:
    return CardMessage(WITHHELD_LINE, [_context(WITHHELD_LINE)])


def age_phrase(age: dt.timedelta) -> str:
    hours = int(age.total_seconds() // _SECONDS_PER_HOUR)
    if hours < 1:
        return "less than an hour ago"
    if hours < _HOURS_UNTIL_DAYS:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return f"{hours // _HOURS_PER_DAY} days ago"


def _asked_at(row: DeferredQuestion) -> dt.datetime:
    return first_posted_at(row.slack_ts, row.created_at)


def bump_text(row: DeferredQuestion, *, now: dt.datetime) -> str:
    """The re-ping posted into the question's own thread: how long it has waited and how to answer."""
    way = "Tap an option above, or reply here." if _view(row).options else "Reply here."
    return f"Still waiting for your decision; I asked {age_phrase(now - _asked_at(row))}. {way}"


def digest_text(rows: list[DeferredQuestion], *, now: dt.datetime) -> str:
    """The daily digest: how many decisions wait and how long the oldest has — counts and age only."""
    age = age_phrase(now - min(_asked_at(row) for row in rows))
    if len(rows) == 1:
        return f"1 decision is waiting for you; it was asked {age}. It is in its own thread."
    return f"{len(rows)} decisions are waiting for you; the oldest was asked {age}. Each one is in its own thread."

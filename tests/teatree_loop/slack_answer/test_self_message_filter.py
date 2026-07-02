"""The reactive answerer skips DMs the user authored themselves (#1941).

The loop's coalescer bundled messages the USER posted to his own bot DM
(his own outbound answer echoes, carrying the "Sent using <@app>" signature)
into a ``slack_answer`` payload and scheduled an answering task for them. A DM
authored by the configured ``slack_user_id`` is by definition not an inbound
question — it must never yield an answering task.
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.models import PendingChatInjection, Task
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_USER_UID = "U0A72P7CK0A"
_COLLEAGUE_UID = "UCOLLEAGUE"
# An imperative → classifier route NEEDS_WORK → would create an answering Task.
_WORK_TEXT = "fix the login bug"


@dataclass
class ReactBackend:
    reactions: list[tuple[str, str, str]] = field(default_factory=list)

    def react(self, *, channel: str, ts: str, emoji: str) -> dict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}


def _row(*, ts: str, user_id: str) -> PendingChatInjection:
    row = PendingChatInjection.record(channel="C1", slack_ts=ts, text=_WORK_TEXT, user_id=user_id)
    assert row is not None
    return row


def _run(backend: ReactBackend):
    return run_slack_answer_cycle(
        messaging_resolver=lambda _overlay: backend,
        self_user_id_resolver=lambda _overlay: _USER_UID,
    )


class TestUserAuthoredMessageIsSkipped:
    def test_never_yields_an_answering_task(self) -> None:
        row = _row(ts="1.0", user_id=_USER_UID)
        backend = ReactBackend()

        report = _run(backend)

        assert Task.objects.count() == 0  # NO answering task for the user's own DM
        assert backend.reactions == []  # not even an :eyes: receipt
        assert report.self_skipped == 1
        assert report.delegated == 0
        row.refresh_from_db()
        assert row.answer_kind == PendingChatInjection.AnswerKind.SELF

    def test_row_leaves_the_answerer_queue(self) -> None:
        _row(ts="1.0", user_id=_USER_UID)

        _run(ReactBackend())

        assert list(PendingChatInjection.loop_unreplied()) == []

    def test_prompt_drain_still_sees_the_row(self) -> None:
        # The self-filter only skips the reactive answerer; the inbound-context
        # drain (consumed_at) must still surface the user's own DM.
        row = _row(ts="1.0", user_id=_USER_UID)

        _run(ReactBackend())

        row.refresh_from_db()
        assert row.consumed_at is None  # untouched — still drainable into a prompt


class TestColleagueAuthoredMessageStillScheduled:
    def test_colleague_needs_work_creates_answering_task(self) -> None:
        # The existing inbound-question path is unaffected: a colleague-authored
        # imperative still delegates to a t3:answerer Task.
        _row(ts="2.0", user_id=_COLLEAGUE_UID)
        backend = ReactBackend()

        report = _run(backend)

        assert report.delegated == 1
        assert report.self_skipped == 0
        assert Task.objects.filter(phase="answering").count() == 1

    def test_row_with_no_author_is_not_skipped(self) -> None:
        # A row lacking user attribution cannot be proven self-authored → kept.
        _row(ts="3.0", user_id="")

        report = _run(ReactBackend())

        assert report.self_skipped == 0
        assert report.delegated == 1

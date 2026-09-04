"""Which question a Slack reply answers, when several are mirrored to one DM.

The owner's DM carries the whole pending backlog — 149 rows at the worst
observed — so "the newest pending question on this channel" is the wrong answer
almost every time. These pin the three rungs that replace it: the ``#<id>``
prefix the backlog digest instructs and nothing used to parse, the reply's
``thread_ts``, and the sole-live-question fallback. With two candidates and
neither an id nor a thread, nothing binds.

Driven through :class:`AskUserQuestionReplyScanner` rather than
:func:`bind_reply` alone: the contract that matters is which row ends up
answered, not which row a helper returns.
"""

import hashlib
import itertools
import json
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from teatree.core import notify as notify_module
from teatree.core.models import BotPing, DmContext, IncomingEvent, PendingChatInjection
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.notify_question_drains import (
    drain_deferred_questions,
    drain_unmirrored_deferred_questions,
    reask_escalated_questions,
)
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_CHANNEL = "D-user"


@dataclass
class FakeMessaging:
    """Self-DM MessagingBackend: every react/post is ungated (self surface)."""

    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    route_token: str = "self"

    def _is_self_dm(self, channel: str) -> bool:
        _ = channel
        return True

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {"ok": True}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/permalink"


def _statement(text: str) -> InboundReading:
    """Read every body as a plain statement, so free text is applied verbatim."""
    _ = text
    return InboundReading(
        intent=InboundIntent.INSTRUCTION,
        answerable=False,
        work_summary="",
        source=ReadingSource.HEURISTIC,
    )


def _question(text: str, *, slack_ts: str, generation: int = 1) -> DeferredQuestion:
    return DeferredQuestion.record(
        text,
        session_id="s",
        run_id="r",
        generation=generation,
        slack_channel=_CHANNEL,
        slack_ts=slack_ts,
    )


def _reply(text: str, *, slack_ts: str, thread_ts: str = "") -> PendingChatInjection:
    row = PendingChatInjection.record(
        channel=_CHANNEL,
        slack_ts=slack_ts,
        text=text,
        context=DmContext(user_id="U1", thread_ts=thread_ts),
    )
    assert row is not None
    return row


def _scan() -> FakeMessaging:
    backend = FakeMessaging()
    AskUserQuestionReplyScanner(backend=backend, overlay="", reader=_statement).scan()
    return backend


class TestThreadedReplyBindsItsOwnQuestion:
    def test_reply_threaded_under_the_older_mirror_answers_the_older_row(self) -> None:
        older = _question("Which DB host?", slack_ts="100.0", generation=1)
        newer = _question("Ship the release?", slack_ts="300.0", generation=2)
        _reply("use postgres-1", slack_ts="400.0", thread_ts="100.0")

        _scan()

        older.refresh_from_db()
        newer.refresh_from_db()
        assert older.answer_text == "use postgres-1"
        assert older.resolved_via == "slack"
        assert newer.is_pending, "the newest question was resolved by a reply threaded under another"

    def test_threaded_reply_acks_and_claims_the_reply_row(self) -> None:
        _question("Which DB host?", slack_ts="100.0", generation=1)
        _question("Ship the release?", slack_ts="300.0", generation=2)
        reply = _reply("use postgres-1", slack_ts="400.0", thread_ts="100.0")

        backend = _scan()

        reply.refresh_from_db()
        assert reply.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY
        assert backend.react_calls == [(_CHANNEL, "400.0", "white_check_mark")]


class TestExplicitIdPrefix:
    def test_hash_id_reply_answers_exactly_that_row(self) -> None:
        first = _question("Which DB host?", slack_ts="100.0", generation=1)
        second = _question("Ship the release?", slack_ts="300.0", generation=2)
        _reply(f"#{first.pk} use postgres-1", slack_ts="400.0")

        _scan()

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.answer_text == "use postgres-1", "the digest's own `#<id> <answer>` format did not bind"
        assert second.is_pending

    def test_hash_id_binds_the_older_row_over_the_newer_one(self) -> None:
        older = _question("Which DB host?", slack_ts="100.0", generation=1)
        newer = _question("Ship the release?", slack_ts="300.0", generation=2)
        _reply(f"#{older.pk}: use postgres-1", slack_ts="400.0")

        _scan()

        older.refresh_from_db()
        newer.refresh_from_db()
        assert older.answer_text == "use postgres-1"
        assert newer.is_pending

    def test_hash_id_naming_a_resolved_row_binds_nothing(self) -> None:
        answered = _question("Which DB host?", slack_ts="100.0", generation=1)
        answered.apply_answer("postgres-9", resolved_via="local")
        still_open = _question("Ship the release?", slack_ts="300.0", generation=2)
        reply = _reply(f"#{answered.pk} use postgres-1", slack_ts="400.0")

        _scan()

        still_open.refresh_from_db()
        reply.refresh_from_db()
        assert still_open.is_pending, "an addressed-but-stale id fell through onto another question"
        assert reply.loop_replied_at is None

    def test_bare_hash_id_with_no_answer_binds_nothing(self) -> None:
        question = _question("Which DB host?", slack_ts="100.0", generation=1)
        _reply(f"#{question.pk}", slack_ts="400.0")

        _scan()

        question.refresh_from_db()
        assert question.is_pending


class TestAmbiguousTopLevelReply:
    def test_two_pending_questions_and_plain_text_resolves_nothing(self) -> None:
        first = _question("Which DB host?", slack_ts="100.0", generation=1)
        second = _question("Ship the release?", slack_ts="300.0", generation=2)
        reply = _reply("use postgres-1", slack_ts="400.0")

        backend = _scan()

        first.refresh_from_db()
        second.refresh_from_db()
        reply.refresh_from_db()
        assert first.is_pending, "an unattributable reply was applied to a guessed question"
        assert second.is_pending, "an unattributable reply was applied to a guessed question"
        assert reply.loop_replied_at is None, "the reply was ✅-acked for an answer nobody recorded"
        assert backend.react_calls == []

    def test_one_pending_question_and_plain_text_still_binds(self) -> None:
        only = _question("Which DB host?", slack_ts="100.0", generation=1)
        _reply("use postgres-1", slack_ts="400.0")

        _scan()

        only.refresh_from_db()
        assert only.answer_text == "use postgres-1"


class TestOptionDigitStillResolves:
    def test_digit_maps_to_its_label_on_the_threaded_question(self) -> None:
        options = [{"label": "Yes"}, {"label": "No"}]
        blob = json.dumps(options, sort_keys=True, ensure_ascii=False)
        older = DeferredQuestion.record(
            "Ship it?",
            options_json=blob,
            options_hash=hashlib.sha256(blob.encode("utf-8")).hexdigest(),
            slack_channel=_CHANNEL,
            slack_ts="100.0",
            generation=1,
        )
        _question("Ship the release?", slack_ts="300.0", generation=2)
        _reply("2", slack_ts="400.0", thread_ts="100.0")

        _scan()

        older.refresh_from_db()
        assert older.answer_text == "No"


def _threading_slack() -> tuple[MagicMock, dict[str, str]]:
    """A Slack DM that models thread RE-PARENTING, and the ``ts -> thread root`` map it builds.

    Slack stamps every reply in a thread with the ROOT message's ``ts``, never
    the ts of the message being replied to. A fake that just hands back a ts
    cannot show that, so a mirror nested one level down looks bindable when it
    is not. ``roots`` records, per posted message, the ts a reply to it would
    carry — which is what makes the at-root posting rule falsifiable here.
    """
    roots: dict[str, str] = {}
    counter = itertools.count(1000)

    def _post(*, channel: str, text: str, thread_ts: str = "", **_kw: object) -> RawAPIDict:
        _ = (channel, text)
        ts = f"{next(counter)}.0"
        roots[ts] = thread_ts or ts
        return {"ok": True, "ts": ts}

    backend = MagicMock()
    backend.open_dm.return_value = _CHANNEL
    backend.post_message.side_effect = _post
    backend.get_permalink.return_value = "https://slack/permalink"
    return backend, roots


def _owner_is_mid_conversation(thread_root: str = "900.0") -> None:
    """Record the inbound event that makes ``_active_dm_thread`` report an open DM thread."""
    IncomingEvent.objects.create(
        source=IncomingEvent.Source.SLACK,
        channel_ref=_CHANNEL,
        thread_ref=thread_root,
        idempotency_key=f"slack:Ev-{thread_root}",
    )


def _mirror_two_questions() -> tuple[DeferredQuestion, DeferredQuestion, dict[str, str]]:
    older = DeferredQuestion.record("Which DB host?", session_id="s", run_id="r", generation=1)
    newer = DeferredQuestion.record("Ship the release?", session_id="s", run_id="r", generation=2)
    backend, roots = _threading_slack()
    with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
        drain_unmirrored_deferred_questions(user_id="U_ME", backend=backend)
    older.refresh_from_db()
    newer.refresh_from_db()
    return older, newer, roots


class TestMirroredQuestionOwnsItsThread:
    """The owner answering a mirrored question in its Slack thread — the common path.

    A mirror nested under whatever DM thread the owner is already in carries a
    mid-thread ``slack_ts``, and Slack stamps the owner's reply with the thread
    ROOT instead. The exact ``thread_ts`` -> mirror-ts join then matches no
    question at all, and with a second one live the sole-question fallback
    refuses as well — so the reply is acknowledged and its answer dropped. The
    mirror therefore posts at the DM root and owns the thread the answer lands in.
    """

    def test_mirror_is_its_own_thread_root(self) -> None:
        _owner_is_mid_conversation()
        older, newer, roots = _mirror_two_questions()

        assert roots[older.slack_ts] == older.slack_ts
        assert roots[newer.slack_ts] == newer.slack_ts

    def test_reply_in_the_questions_thread_answers_that_question(self) -> None:
        _owner_is_mid_conversation()
        older, newer, roots = _mirror_two_questions()
        assert older.slack_ts < newer.slack_ts, "the older mirror must not also be the newest row"

        _reply("use postgres-1", slack_ts="2000.0", thread_ts=roots[older.slack_ts])
        _scan()

        older.refresh_from_db()
        newer.refresh_from_db()
        assert older.answer_text == "use postgres-1", "a reply in the question's own Slack thread bound nothing"
        assert newer.is_pending, "the newest mirrored question absorbed a reply threaded under an older one"

    def test_reply_in_the_owners_own_thread_binds_nothing(self) -> None:
        _owner_is_mid_conversation()
        older, newer, _roots = _mirror_two_questions()

        reply = _reply("use postgres-1", slack_ts="2000.0", thread_ts="900.0")
        backend = _scan()

        older.refresh_from_db()
        newer.refresh_from_db()
        reply.refresh_from_db()
        assert older.is_pending, "a reply in a thread hosting no question was guessed onto one"
        assert newer.is_pending, "a reply in a thread hosting no question was guessed onto one"
        assert reply.loop_replied_at is None, "the reply was ✅-acked for an answer nobody recorded"
        assert backend.react_calls == []


def _recording_slack(first_ts: int, roots: dict[str, str]) -> tuple[MagicMock, list[str]]:
    """A Slack DM that re-parents like the real one, recording every post in order.

    ``roots`` is shared with the mirror posts so a reply's ``thread_ts`` is read out
    of the same re-parenting model the mirrors were posted under — never assumed.
    """
    posted: list[str] = []
    counter = itertools.count(first_ts)

    def _post(*, channel: str, text: str, thread_ts: str = "", **_kw: object) -> RawAPIDict:
        _ = (channel, text)
        ts = f"{next(counter)}.0"
        roots[ts] = thread_ts or ts
        posted.append(ts)
        return {"ok": True, "ts": ts}

    backend = MagicMock()
    backend.open_dm.return_value = _CHANNEL
    backend.get_permalink.return_value = "https://slack/permalink"
    backend.post_message.side_effect = _post
    return backend, posted


class TestAReplyToAReAskBumpBindsItsQuestion:
    """The whole point of the re-ask lane: the bump is posted where an answer lands.

    A recurring nag posted anywhere but the question's own thread is a question asked
    in a channel whose replies bind by NO rung — the exact thread join misses, and at
    a backlog above one the sole-live-question rung refuses — so the owner is asked
    again and again and every reply is acknowledged and dropped.
    """

    def test_the_bump_is_posted_into_the_questions_own_thread(self) -> None:
        _owner_is_mid_conversation()
        older, _newer, roots = _mirror_two_questions()
        backend, _posted = _recording_slack(5000, roots)

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            reask_escalated_questions(user_id="U_ME", backend=backend)

        threads = [call.kwargs["thread_ts"] for call in backend.post_message.call_args_list]
        assert older.slack_ts in threads
        assert "900.0" not in threads, "the bump nested under the owner's active DM thread instead"

    def test_a_reply_under_the_bump_answers_that_question(self) -> None:
        _owner_is_mid_conversation()
        older, newer, roots = _mirror_two_questions()
        assert older.slack_ts < newer.slack_ts, "the older mirror must not also be the newest row"
        backend, posted = _recording_slack(5000, roots)

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            reask_escalated_questions(user_id="U_ME", backend=backend)

        # Oldest-first, so the first bump is the older question's; the reply carries
        # whatever thread root the fake says a reply to THAT message would carry.
        assert len(posted) == 2
        _reply("use postgres-1", slack_ts="9000.0", thread_ts=roots[posted[0]])
        _scan()

        older.refresh_from_db()
        newer.refresh_from_db()
        assert older.answer_text == "use postgres-1", "a reply under the re-ask bump bound nothing"
        assert newer.is_pending, "the bump for one question absorbed the answer to another"


class TestAResurfacedQuestionIsAnswerable:
    """The away->present resurface must land where a reply to it binds, or not at all.

    It used to thread into whatever DM thread the owner was in and never stamp a
    mirror, so the resurfaced copy carried no bindable identity in either direction:
    a reply to it matched no question by thread, and with a backlog above one the
    sole-question rung refused. The owner was re-asked and their answer dropped.
    """

    def test_a_reply_to_a_resurfaced_mirrored_question_answers_it(self) -> None:
        _owner_is_mid_conversation()
        older, newer, roots = _mirror_two_questions()
        BotPing.objects.all().delete()
        backend, posted = _recording_slack(7000, roots)

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            drain_deferred_questions(user_id="U_ME", backend=backend)

        assert len(posted) == 2
        _reply("use postgres-1", slack_ts="9500.0", thread_ts=roots[posted[0]])
        _scan()

        older.refresh_from_db()
        newer.refresh_from_db()
        assert older.answer_text == "use postgres-1", "a reply to the resurfaced question bound nothing"
        assert newer.is_pending

    def test_an_unmirrored_row_is_resurfaced_at_root_and_stamped(self) -> None:
        _owner_is_mid_conversation()
        row = DeferredQuestion.record("Which DB host?", session_id="s", run_id="r")
        roots: dict[str, str] = {}
        backend, posted = _recording_slack(8000, roots)

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            drain_deferred_questions(user_id="U_ME", backend=backend)

        row.refresh_from_db()
        assert row.slack_ts == posted[0], "the resurfaced copy left the row with no mirror a reply can name"
        assert roots[row.slack_ts] == row.slack_ts, "the stamped ts is mid-thread, so no reply can carry it"

        _reply("use postgres-1", slack_ts="9600.0", thread_ts=roots[row.slack_ts])
        _scan()

        row.refresh_from_db()
        assert row.answer_text == "use postgres-1"


class TestReplyThreadedUnderANonQuestion:
    """A reply that named a thread has already stated its referent (#4527).

    ``sole_for_reply`` is the fallback for a TOP-LEVEL reply — with a single live
    mirror such a reply cannot be for anything else. A reply carrying a
    ``thread_ts`` that matches no mirror is a different message: it answers the
    watchdog/info DM it is threaded under. Binding it to the sole live question
    consumed the owner's INSTRUCTION as that question's answer and ✅-acked it, so
    the instruction reached no consumer at all — the silent drop that lost nine
    owner messages in one audited fortnight.
    """

    def test_instruction_threaded_under_an_info_dm_binds_nothing(self) -> None:
        only = _question("Which DB host?", slack_ts="100.0", generation=1)
        reply = _reply("fix all of them", slack_ts="400.0", thread_ts="250.0")

        backend = _scan()

        only.refresh_from_db()
        reply.refresh_from_db()
        assert only.is_pending, "an instruction threaded under a non-question was applied as its answer"
        assert reply.loop_replied_at is None, "the instruction was claimed, so the cycle never dispatched it"
        assert backend.react_calls == [], "the owner was ✅-acked for an answer nobody recorded"

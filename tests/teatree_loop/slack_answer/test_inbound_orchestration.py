"""The inbound loop ORCHESTRATES: it reads, dedupes, dispatches, and never over-claims.

The failure this module pins: the owner replied twice in a DM thread, the loop
put :eyes: on both and :white_check_mark: on the second — a question — and did
nothing else. The question was never answered, no work was dispatched, and the
:white_check_mark: told the owner it had been handled. Three separate defects:

* a receipt reaction and a completion reaction were the SAME emoji, so "seen"
    and "done" were indistinguishable on the only surface the owner reads;
* an inbound message that implied work dispatched nothing; and
* nothing ever asked whether a lane was already on it.

The four claims below are the contract:

1. a message implying work dispatches EXACTLY ONE task;
2. the same request, when a live lane already covers it, dispatches NONE and
    says so in-thread;
3. a question gets an ANSWER, not a bare reaction; and
4. nothing is marked complete that was not — :white_check_mark: is placed only
    behind a verified, delivered answer.
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.models import DmContext, PendingChatInjection, Task, Ticket
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.loop.slack_answer.orchestration import WorkOrigin, dispatch_work, find_coverage
from teatree.loop.slack_answer.vocabulary import InboundReaction
from teatree.types import RawAPIDict
from teatree.utils.url_slug import is_synthetic_loop_umbrella_url

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_CHANNEL = "D-owner"


@dataclass
class RecordingBackend:
    """In-memory ``MessagingBackend`` that records every react and thread post."""

    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        return {"ok": True}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return _CHANNEL

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/p1"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True, "user_id": "U-bot", "bot_id": "B-bot"}

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = channel
        return {"ts": ts}

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        """Reflect the posts this backend recorded, so the read-back sees a real delivery."""
        _ = channel
        return [{"ts": thread_ts, "text": text, "bot_id": "B-bot"} for _c, _t, text in self.replies]

    @property
    def emojis(self) -> list[str]:
        return [emoji for _channel, _ts, emoji in self.reactions]


def _row(text: str, *, ts: str, user_id: str = "U-owner") -> PendingChatInjection:
    row = PendingChatInjection.record(channel=_CHANNEL, slack_ts=ts, text=text, context=DmContext(user_id=user_id))
    assert row is not None
    return row


def _resolver(backend: RecordingBackend):
    return lambda _overlay: backend


def _reader(reading: InboundReading):
    return lambda _text: reading


def _work(summary: str, *, intent: InboundIntent = InboundIntent.INSTRUCTION) -> InboundReading:
    return InboundReading(
        intent=intent,
        answerable=False,
        work_summary=summary,
        source=ReadingSource.MODEL,
        rationale="test fixture",
    )


class TestWorkImplyingMessageDispatchesExactlyOneTask:
    """Claim 1 — an instruction that implies work mints one task, no more."""

    def test_one_task_is_dispatched(self) -> None:
        _row("the interest-rate rounding is still wrong on the offer PDF", ts="1.0")
        backend = RecordingBackend()

        report = run_slack_answer_cycle(
            messaging_resolver=_resolver(backend),
            reader=_reader(_work("fix interest-rate rounding on the offer PDF")),
        )

        assert report.dispatched == 1
        assert Task.objects.filter(status=Task.Status.PENDING).count() == 1

    def test_the_dispatched_task_carries_the_request(self) -> None:
        """A fire-and-forget dispatch is not tracked work — the task names what it is for."""
        _row("the interest-rate rounding is still wrong on the offer PDF", ts="1.0")
        backend = RecordingBackend()

        run_slack_answer_cycle(
            messaging_resolver=_resolver(backend),
            reader=_reader(_work("fix interest-rate rounding on the offer PDF")),
        )

        task = Task.objects.get()
        assert "interest-rate" in task.execution_reason.lower()
        assert task.ticket.extra["slack_answer"]["slack_ts"] == "1.0"
        assert task.subject == "fix interest-rate rounding on the offer PDF", (
            "the lane is not named by the interpreted request, so nobody scanning the queue can see what it is"
        )
        assert task.ticket.extra["slack_answer"]["fingerprint"], (
            "the lane carries no dedupe key, so the next report of the same problem mints a rival"
        )

    def test_the_dispatch_is_signalled_as_in_flight_not_done(self) -> None:
        """A dispatched lane is IN FLIGHT — never the completion emoji."""
        _row("the interest-rate rounding is still wrong on the offer PDF", ts="1.0")
        backend = RecordingBackend()

        run_slack_answer_cycle(
            messaging_resolver=_resolver(backend),
            reader=_reader(_work("fix interest-rate rounding on the offer PDF")),
        )

        assert InboundReaction.IN_FLIGHT in backend.emojis
        assert InboundReaction.DONE not in backend.emojis


class TestAlreadyCoveredDispatchesNothingAndSaysSo:
    """Claim 2 — a live lane on the same request wins; the loop reports it instead."""

    def test_no_second_task_and_the_reply_names_the_lane(self) -> None:
        summary = "fix interest-rate rounding on the offer PDF"
        _row("the interest-rate rounding is still wrong on the offer PDF", ts="1.0")
        backend = RecordingBackend()
        run_slack_answer_cycle(messaging_resolver=_resolver(backend), reader=_reader(_work(summary)))
        first = Task.objects.get()

        # Same request, different words, a later message.
        _row("did anyone look at the interest-rate rounding on the PDF yet", ts="2.0")
        second_backend = RecordingBackend()
        report = run_slack_answer_cycle(
            messaging_resolver=_resolver(second_backend),
            reader=_reader(_work(summary)),
        )

        assert report.dispatched == 0
        assert report.covered == 1
        assert Task.objects.count() == 1, "a rival lane was minted for a request already covered"
        assert second_backend.replies, "the owner was told nothing about the request being covered"
        body = second_backend.replies[0][2]
        assert str(first.pk) in body, f"the reply does not name the covering lane: {body!r}"

    def test_coverage_never_claims_completion(self) -> None:
        summary = "fix interest-rate rounding on the offer PDF"
        _row("the interest-rate rounding is still wrong on the offer PDF", ts="1.0")
        run_slack_answer_cycle(messaging_resolver=_resolver(RecordingBackend()), reader=_reader(_work(summary)))

        _row("did anyone look at the interest-rate rounding on the PDF yet", ts="2.0")
        backend = RecordingBackend()
        run_slack_answer_cycle(messaging_resolver=_resolver(backend), reader=_reader(_work(summary)))

        assert InboundReaction.DONE not in backend.emojis, "an in-flight lane was reported as done"


class TestQuestionGetsAnAnswerNotABareReaction:
    """Claim 3 — the owner reads Slack; an answer that is not posted is not delivered."""

    _QUESTION = InboundReading(
        intent=InboundIntent.QUESTION,
        answerable=True,
        work_summary="",
        source=ReadingSource.MODEL,
        rationale="answerable from teatree state",
    )

    def test_an_answerable_question_is_answered_in_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "teatree.loop.slack_answer.cycle.build_simple_answer",
            lambda _row: "3 open MRs, none blocked.",
        )
        _row("beside that everything fixed?", ts="1.0")
        backend = RecordingBackend()

        report = run_slack_answer_cycle(
            messaging_resolver=_resolver(backend),
            reader=_reader(self._QUESTION),
        )

        assert report.answered_simple == 1
        assert backend.replies, "the question got a reaction and no answer"
        assert "3 open MRs" in backend.replies[0][2]

    def test_an_unanswerable_question_is_dispatched_not_swallowed(self) -> None:
        """A question teatree cannot answer cheaply becomes work — never a silent drop."""
        _row("why is the acme pipeline still red?", ts="1.0")
        backend = RecordingBackend()

        report = run_slack_answer_cycle(
            messaging_resolver=_resolver(backend),
            reader=_reader(
                InboundReading(
                    intent=InboundIntent.QUESTION,
                    answerable=False,
                    work_summary="find out why the acme pipeline is red",
                    source=ReadingSource.MODEL,
                    rationale="needs investigation",
                )
            ),
        )

        assert report.dispatched == 1
        assert Task.objects.filter(status=Task.Status.PENDING).count() == 1


class TestNothingIsMarkedCompleteThatWasNot:
    """Claim 4 — the reported failure. A completion emoji is a completion claim."""

    def test_an_unanswered_question_never_gets_the_completion_emoji(self) -> None:
        """The exact reported failure: :white_check_mark: on a question nobody answered."""
        _row("beside that everything fixed?", ts="1.0")
        backend = RecordingBackend()

        run_slack_answer_cycle(
            messaging_resolver=_resolver(backend),
            reader=_reader(
                InboundReading(
                    intent=InboundIntent.QUESTION,
                    answerable=False,
                    work_summary="report whether the remaining issues are fixed",
                    source=ReadingSource.MODEL,
                    rationale="needs a live check",
                )
            ),
        )

        assert InboundReaction.DONE not in backend.emojis, (
            "a question that was never answered was marked done — the owner stops waiting on that signal"
        )

    def test_receipt_and_completion_are_different_emojis(self) -> None:
        """`seen` and `done` must not collide; the receipt emoji is its own symbol."""
        assert InboundReaction.RECEIVED != InboundReaction.DONE
        assert InboundReaction.IN_FLIGHT not in {InboundReaction.RECEIVED, InboundReaction.DONE}
        assert InboundReaction.NOTED != InboundReaction.DONE

    def test_a_bare_acknowledgement_is_noted_not_completed(self) -> None:
        """`thanks` closes nothing — it gets the NOTED symbol, never the completion one."""
        _row("thanks", ts="1.0")
        backend = RecordingBackend()

        run_slack_answer_cycle(
            messaging_resolver=_resolver(backend),
            reader=_reader(
                InboundReading(
                    intent=InboundIntent.NOISE,
                    answerable=False,
                    work_summary="",
                    source=ReadingSource.MODEL,
                    rationale="pure acknowledgement",
                )
            ),
        )

        assert InboundReaction.NOTED in backend.emojis
        assert InboundReaction.DONE not in backend.emojis

    def test_completion_emoji_requires_a_delivered_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An answer the read-back cannot see is not delivered — so nothing is stamped done."""
        monkeypatch.setattr(
            "teatree.loop.slack_answer.cycle.build_simple_answer",
            lambda _row: "3 open MRs, none blocked.",
        )
        monkeypatch.setattr(
            "teatree.loop.slack_answer.cycle.verify_reply_visible",
            lambda *_args, **_kwargs: False,
        )
        row = _row("what are you working on?", ts="1.0")
        backend = RecordingBackend()

        run_slack_answer_cycle(
            messaging_resolver=_resolver(backend),
            reader=_reader(
                InboundReading(
                    intent=InboundIntent.QUESTION,
                    answerable=True,
                    work_summary="",
                    source=ReadingSource.MODEL,
                    rationale="status question",
                )
            ),
        )

        assert InboundReaction.DONE not in backend.emojis
        row.refresh_from_db()
        assert row.loop_replied_at is None, "an unverified answer was stamped as replied"


class TestTheDispatchedRowIsNeverAnUnfindableShell:
    """Claim 5 (#4527) — the lane's Ticket must not look like tracked work nobody can find.

    Every dispatch minted a row with a blank ``issue_url`` AND a blank
    ``short_description``: intake discovers candidates from the forge, so such a
    row can never be admitted, claimed, or found again. Fifty accumulated, each
    the only surviving record of one owner request.
    """

    def test_the_row_is_described_and_anchored(self) -> None:
        _row("the interest-rate rounding is still wrong on the offer PDF", ts="1.0")

        run_slack_answer_cycle(
            messaging_resolver=_resolver(RecordingBackend()),
            reader=_reader(_work("fix interest-rate rounding on the offer PDF")),
        )

        ticket = Task.objects.get().ticket
        assert ticket.short_description == "fix interest-rate rounding on the offer PDF", (
            "the row carries no description, so every surface renders it as a nameless card"
        )
        assert is_synthetic_loop_umbrella_url(ticket.issue_url), (
            f"the row is not anchored on the synthetic-loop umbrella: {ticket.issue_url!r}"
        )
        assert ticket.issue_url.endswith("/dm"), (
            "the anchor fragment ends in digits, so derive_issue_number stamps the Slack ts as an issue number"
        )
        assert ticket.issue_number == "", f"a bogus forge issue number was derived: {ticket.issue_number!r}"

    def test_the_conversation_row_is_not_mistaken_for_tracked_work(self) -> None:
        """The anchor is bookkeeping — ``is_admissible`` is what says "intake could find this"."""
        _row("the interest-rate rounding is still wrong on the offer PDF", ts="1.0")

        run_slack_answer_cycle(
            messaging_resolver=_resolver(RecordingBackend()),
            reader=_reader(_work("fix interest-rate rounding on the offer PDF")),
        )

        assert not Task.objects.get().ticket.is_admissible(), (
            "the conversation row claims to be findable work; announcing it as `tracking as ticket N` is the trap"
        )

    def test_a_re_dispatch_of_the_same_message_reuses_one_row(self) -> None:
        """The Slack ts is the identity — a retried cycle must not mint a second row for it."""
        reading = _work("fix interest-rate rounding on the offer PDF")
        _row("the interest-rate rounding is still wrong on the offer PDF", ts="1.0")
        origin = WorkOrigin(overlay="", channel=_CHANNEL, slack_ts="1.0", coalesced_ts=("1.0",), text="rounding")

        first = dispatch_work(reading=reading, fingerprint="fp", origin=origin)
        second = dispatch_work(reading=reading, fingerprint="fp", origin=origin)

        assert first.ticket_id == second.ticket_id, "a retried dispatch forked a second row for one message"


class TestADivergedLaneOverlayIsRepairedSoCoverageStillFindsIt:
    """``find_coverage`` scans ``ticket__overlay=<queue overlay>``, so a diverged row is invisible.

    The row diverges when it was minted before the queue's overlay was known and
    ``Ticket.save`` inferred one from the umbrella anchor. Nothing re-dispatches a
    lane it cannot see, so the next report of the same problem mints a rival.
    """

    def _dispatch(self, overlay: str) -> Task:
        return dispatch_work(
            reading=_work("fix interest-rate rounding on the offer PDF"),
            fingerprint="fp-rounding",
            origin=WorkOrigin(
                overlay=overlay, channel=_CHANNEL, slack_ts="1.0", coalesced_ts=("1.0",), text="rounding"
            ),
        )

    def test_a_re_dispatch_repoints_the_existing_row_at_the_queue_overlay(self) -> None:
        self._dispatch("t3-other")

        task = self._dispatch("t3-teatree")

        assert Ticket.objects.get(pk=task.ticket_id).overlay == "t3-teatree"

    def test_the_repaired_lane_is_visible_to_find_coverage(self) -> None:
        self._dispatch("t3-other")
        self._dispatch("t3-teatree")

        coverage = find_coverage(
            fingerprint="fp-rounding",
            slack_ts="1.0",
            coalesced_ts=("1.0",),
            overlay="t3-teatree",
            text="rounding",
        )

        assert coverage is not None, "the lane is invisible to the queue that owns it, so a rival will be minted"

"""Integration tests for the reactive Slack-answer cycle (#1014).

Real :class:`PendingChatInjection` rows, real classifier, real Task/Ticket
DB writes. Only the Slack network is faked (a recording backend). The
load-bearing assertions:

- :eyes: is reacted **exactly once** per row across cycle re-runs.
- ACK_ONLY posts a reaction, NO thread reply, stamps ``answer_kind=ack``.
- SIMPLE Stage A posts a thread reply with no LLM and stamps ``simple``.
- NEEDS_WORK creates **exactly one** PENDING ``t3:answerer`` Task and
    stamps ``delegated``. NO thread reply (#1155): the :eyes: receipt
    is the only acknowledgement on the delegated path.
- A post/readback failure leaves the row unanswered (retry next cycle).
- One bad row never blocks the others.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from teatree.core.models import PendingChatInjection, Task, Ticket
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.types import RawAPIDict

pytestmark = pytest.mark.django_db


@dataclass
class RecordingBackend:
    """In-memory MessagingBackend recording react / post_reply calls."""

    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)
    permalink_ok: bool = True
    post_reply_raises: bool = False

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
        if self.post_reply_raises:
            msg = "slack 503"
            raise RuntimeError(msg)
        self.replies.append((channel, ts, text))
        return {"ok": True}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D1"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/p1" if self.permalink_ok else ""

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


def _row(text: str, ts: str = "1.0", overlay: str = "") -> PendingChatInjection:
    row = PendingChatInjection.record(channel="C1", slack_ts=ts, text=text, overlay=overlay)
    assert row is not None
    return row


def _resolver(backend: RecordingBackend):
    return lambda _overlay: backend


class TestEyesReaction:
    def test_eyes_reacted_exactly_once_across_reruns(self) -> None:
        row = _row("thanks")
        backend = RecordingBackend()

        run_slack_answer_cycle(messaging_resolver=_resolver(backend))
        run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        eyes = [r for r in backend.reactions if r[2] in {"eyes", ":eyes:", "👀"}]
        assert len(eyes) == 1
        row.refresh_from_db()
        assert row.eyes_reacted_at is not None


class TestAckOnly:
    def test_ack_reacts_and_marks_without_thread_reply(self) -> None:
        row = _row("thanks!")
        backend = RecordingBackend()

        report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert backend.replies == []  # NO thread post for an ack
        row.refresh_from_db()
        assert row.answer_kind == "ack"
        assert row.loop_replied_at is not None
        assert report.acked == 1


class TestSimple:
    def test_simple_stage_a_posts_reply_no_llm(self) -> None:
        row = _row("what's the status?")
        backend = RecordingBackend()

        with (
            patch(
                "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
                return_value="overlay=acme\nticket=#1\n",
            ),
            patch("teatree.loop.slack_answer.simple_answer._run_haiku") as haiku,
        ):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        haiku.assert_not_called()
        assert len(backend.replies) == 1
        channel, ts, text = backend.replies[0]
        assert (channel, ts) == ("C1", "1.0")  # threaded under the user msg
        assert "overlay=acme" in text
        row.refresh_from_db()
        assert row.answer_kind == "simple"
        assert report.answered_simple == 1

    def test_simple_not_stamped_when_readback_fails(self) -> None:
        row = _row("what's the status?")
        backend = RecordingBackend(permalink_ok=False)

        with patch(
            "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
            return_value="overlay=acme\nticket=#1\n",
        ):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        row.refresh_from_db()
        assert row.loop_replied_at is None  # retry next cycle (fail-safe)

    def test_simple_not_stamped_when_post_raises(self) -> None:
        row = _row("what's the status?")
        backend = RecordingBackend(post_reply_raises=True)

        with patch(
            "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
            return_value="overlay=acme\nticket=#1\n",
        ):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        row.refresh_from_db()
        assert row.loop_replied_at is None


class TestNeedsWorkDelegation:
    def test_creates_one_pending_answerer_task_and_stamps_delegated(self) -> None:
        row = _row("fix the failing pipeline")
        backend = RecordingBackend()

        report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        tasks = Task.objects.filter(phase="answering", status=Task.Status.PENDING)
        assert tasks.count() == 1
        task = tasks.get()
        assert task.ticket.role == Ticket.Role.AUTHOR
        assert "1.0" in task.execution_reason
        # NO thread reply on the delegated path (#1155). The :eyes: react
        # fired earlier in the cycle is the only acknowledgement.
        assert backend.replies == []
        row.refresh_from_db()
        assert row.answer_kind == "delegated"
        assert report.delegated == 1

    def test_rerun_does_not_create_a_second_task(self) -> None:
        _row("investigate the flaky test")
        backend = RecordingBackend()

        run_slack_answer_cycle(messaging_resolver=_resolver(backend))
        run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert Task.objects.filter(phase="answering").count() == 1


class TestLinkOnlyDmDoesNotMisfireStatusline:
    """A link-only inbound DM must NOT get the default statusline reply.

    An X.com status link contains the substring "status", which used to
    mis-classify the DM as a SIMPLE status request, so the cycle posted
    the statusline content threaded under the unrelated link DM. A bare
    URL has no status intent → delegate, never a threaded statusline.
    """

    def test_x_link_only_dm_posts_no_statusline_and_no_threaded_reply(self) -> None:
        row = _row("https://x.com/user/status/1780726427261379")
        backend = RecordingBackend()

        with patch(
            "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
            return_value="self-improve 10m · tick 11m\nstarted · coded · tested\n",
        ):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert backend.replies == []  # no statusline dump, no threaded reply
        row.refresh_from_db()
        assert row.answer_kind != "simple"
        assert report.answered_simple == 0
        tasks = Task.objects.filter(phase="answering", status=Task.Status.PENDING)
        assert tasks.count() == 1  # left for the real handler (delegated)


class TestRowIsolation:
    def test_one_bad_row_does_not_block_others(self) -> None:
        bad = _row("thanks", ts="1.0")
        good = _row("ok", ts="2.0")
        backend = RecordingBackend()

        # Make the first row's react raise, the rest must still process.
        original_react = backend.react
        calls: list[str] = []

        def flaky_react(*, channel: str, ts: str, emoji: str) -> RawAPIDict:
            calls.append(ts)
            if ts == "1.0" and len(calls) == 1:
                msg = "boom"
                raise RuntimeError(msg)
            return original_react(channel=channel, ts=ts, emoji=emoji)

        backend.react = flaky_react  # type: ignore[method-assign]
        report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        good.refresh_from_db()
        assert good.answer_kind == "ack"
        assert report.errors >= 1
        _ = bad


class TestBoundedBatch:
    def test_at_most_ten_rows_per_cycle(self) -> None:
        for i in range(12):
            _row("thanks", ts=f"{i}.0")
        backend = RecordingBackend()

        report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.processed == 10

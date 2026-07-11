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

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


_BOT_UID = "UBOT"
_USER_UID = "UUSER"


@dataclass
class RecordingBackend:
    """In-memory MessagingBackend modelling Slack thread re-parenting (#2061).

    ``post_reply(ts=<user msg ts>)`` re-parents to the thread ROOT, exactly
    as Slack does: the posted bot reply is appended to ``thread_replies``
    under the user message's resolved root, and ``fetch_thread_replies``
    reads from there. ``message_meta`` maps a user-message ts to its
    ``{ts, thread_ts}`` so ``resolve_thread_root`` can canonicalise a
    non-root reply ts up to its root. ``read_after_post`` controls whether
    the just-posted reply is visible on read-back (models a transient
    read failure for the conservative-retry path).
    """

    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)
    message_meta: dict[str, RawAPIDict] = field(default_factory=dict)
    thread_replies: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    read_after_post: bool = True
    post_reply_raises: bool = False

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = channel
        return self.message_meta.get(ts, {})

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        _ = channel
        if not self.read_after_post:
            return []
        return list(self.thread_replies.get(thread_ts, []))

    def auth_test(self) -> RawAPIDict:
        return {"ok": True, "user_id": _BOT_UID}

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def _root_of(self, ts: str) -> str:
        meta = self.message_meta.get(ts, {})
        thread_ts = meta.get("thread_ts")
        return thread_ts if isinstance(thread_ts, str) and thread_ts else ts

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        if self.post_reply_raises:
            msg = "slack 503"
            raise RuntimeError(msg)
        self.replies.append((channel, ts, text))
        root = self._root_of(ts)
        posted = {"ts": f"{ts}-bot", "user": _BOT_UID, "text": text, "thread_ts": root}
        self.thread_replies.setdefault(root, []).append(posted)
        return {"ok": True}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D1"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/p1"

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
            patch("teatree.loop.slack_answer.simple_answer._run_cheap_turn") as cheap,
        ):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        cheap.assert_not_called()
        assert len(backend.replies) == 1
        channel, ts, text = backend.replies[0]
        assert (channel, ts) == ("C1", "1.0")  # threaded under the user msg
        assert "overlay=acme" in text
        row.refresh_from_db()
        assert row.answer_kind == "simple"
        assert report.answered_simple == 1

    def test_simple_not_stamped_when_readback_fails(self) -> None:
        row = _row("what's the status?")
        backend = RecordingBackend(read_after_post=False)

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


class TestNonRootUserMessageReadBack:
    """Non-root user-message read-back resolves to the thread ROOT (#2061).

    A user message that is itself a thread reply (non-root ts) must
    verify and dedup against the thread ROOT, not the user-message ts.

    The bot reply re-parents to the root, so a read-back keyed on the
    non-root user-message ts misses it: the verification would wrongly
    stamp the delivered reply as absent (→ unstamped, re-posts next cycle =
    duplicate) and a second cooperating answerer's dedup would see "no
    prior reply" and post a duplicate. Resolving the root first makes both
    reads find the just-posted reply.
    """

    _ROOT = "1780770410.451969"
    _USER_REPLY_TS = "1780772700.000100"

    def _non_root_row(self) -> PendingChatInjection:
        return _row("what's the status?", ts=self._USER_REPLY_TS)

    def _non_root_backend(self) -> RecordingBackend:
        return RecordingBackend(
            message_meta={self._USER_REPLY_TS: {"ts": self._USER_REPLY_TS, "thread_ts": self._ROOT, "user": _USER_UID}}
        )

    def test_delivered_reply_under_root_is_verified_and_stamped(self) -> None:
        row = self._non_root_row()
        backend = self._non_root_backend()

        with patch(
            "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
            return_value="overlay=acme\nticket=#1\n",
        ):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert len(backend.replies) == 1  # exactly one answer posted
        assert self._ROOT in backend.thread_replies  # re-parented to the root
        row.refresh_from_db()
        assert row.answer_kind == "simple"
        assert row.loop_replied_at is not None  # verification found it under the root

    def test_rerun_does_not_post_a_duplicate_answer_under_root(self) -> None:
        """A second answerer (fresh row, no shared CAS) must dedup on the root.

        Models #2061's cross-agent duplicate: a reply already exists under
        the root; the dedup read keyed on the root finds it and skips the
        post. Keyed on the non-root user-message ts it would see nothing
        and post a duplicate.
        """
        backend = self._non_root_backend()
        backend.thread_replies[self._ROOT] = [
            {"ts": f"{self._ROOT}-prior", "user": _BOT_UID, "text": "overlay=acme", "thread_ts": self._ROOT}
        ]
        row = self._non_root_row()

        with patch(
            "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
            return_value="overlay=acme\nticket=#1\n",
        ):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert backend.replies == []  # dedup short-circuited the post
        row.refresh_from_db()
        assert row.answer_kind == "simple"  # treated as already-answered
        assert row.loop_replied_at is not None


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

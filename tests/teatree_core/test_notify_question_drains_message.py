"""The DeferredQuestion resurface DM must be Slack-reply-only, never a host CLI.

The owner reads Slack DMs and has NO host-CLI access — every interaction is in
Slack. The resurface/mirror message the drains post therefore must NOT tell the
owner to run ``t3 <overlay> questions answer …``; the owner just replies in the
thread and the reply scanner binds the answer.
"""

import dataclasses
from datetime import timedelta
from typing import cast
from unittest.mock import patch

import httpx
from django.test import TestCase
from django.utils import timezone

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption
from teatree.core.models import BotPing, DeferredQuestion
from teatree.core.notify_question_drains import drain_deferred_questions, drain_unmirrored_deferred_questions
from teatree.core.notify_types import DELIVERED
from teatree.core.owner_question_message import render_blocks, render_text
from teatree.slack_mrkdwn import WRAP_WIDTH
from tests._owner_channel import OWNER_CARD, OWNER_DECISION, owner_card


class TestTheCardHasNoHostCli(TestCase):
    def test_message_carries_no_t3_cli_instruction(self) -> None:
        row = DeferredQuestion.record("Should I merge the widget change?", session_id="s1", **OWNER_DECISION)

        text = render_text(row)

        # No host-CLI instruction of any kind — the owner cannot run one.
        assert "t3 " not in text
        assert "questions answer" not in text
        assert "Answer with" not in text
        # It DOES tell the owner to reply in the thread instead.
        assert "reply" in text.lower()
        assert "thread" in text.lower()

    def test_message_still_renders_question_and_options(self) -> None:
        card = dataclasses.replace(
            OWNER_CARD,
            options=(
                CardOption("canary", "I start with ten percent.", recommended=True),
                CardOption("full", "I ship to all."),
            ),
        )
        row = DeferredQuestion.record("Pick a rollout?", card=card, session_id="s2")

        text = render_text(row)

        assert "Pick a rollout?" in text
        assert "[canary] (recommended) - I start with ten percent." in text
        assert "t3 " not in text


class TestTheFirstPostIsTheCardWithButtons(TestCase):
    """The first post of an owner question goes out through the real Slack backend, byte for byte."""

    LINK = "<https://git.acme.example/widgets/issues/42|the widget issue>"

    def _post_first(self, monkeypatch_calls: list[tuple[str, dict]]) -> DeferredQuestion:
        def fake_post(url: str, **kwargs: object) -> httpx.Response:
            method = url.rsplit("/", maxsplit=1)[-1]
            monkeypatch_calls.append((method, cast("dict", kwargs["json"])))
            bodies = {
                "conversations.open": {"ok": True, "channel": {"id": "D0DEMOOWNER"}},
                "chat.postMessage": {"ok": True, "ts": "1700000000.000100", "channel": "D0DEMOOWNER"},
            }
            return httpx.Response(200, json=bodies.get(method, {"ok": True}), request=httpx.Request("POST", url))

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            body = {"ok": True, "permalink": "https://acme.slack.example/archives/D0DEMOOWNER/p1700000000000100"}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        row = DeferredQuestion.record(
            f"Is {self.LINK} ready to be released to the whole acme team, or should it wait a little longer?",
            card=dataclasses.replace(OWNER_CARD, why="It was filed a week ago and nobody has looked at it since then."),
        )
        with patch.object(slack_http.httpx, "post", fake_post), patch.object(slack_http.httpx, "get", fake_get):
            backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U0DEMOOWNER")
            drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)
        row.refresh_from_db()
        return row

    def test_the_first_post_is_the_card_with_buttons(self) -> None:
        calls: list[tuple[str, dict]] = []

        row = self._post_first(calls)

        [(_, payload)] = [call for call in calls if call[0] == "chat.postMessage"]
        assert payload["text"] == render_text(row)
        assert payload["blocks"] == render_blocks(row)
        assert payload["text"].splitlines()[0].startswith(f"Is {self.LINK} ready to be released")
        assert "thread_ts" not in payload
        assert not payload["text"].startswith(":question:")
        assert "[Yes] (recommended) - I go ahead." in payload["text"]
        assert row.slack_ts == "1700000000.000100"
        assert row.slack_channel == "D0DEMOOWNER"

    def test_no_line_of_the_card_is_wrapped_or_split(self) -> None:
        calls: list[tuple[str, dict]] = []

        row = self._post_first(calls)

        [(_, payload)] = [call for call in calls if call[0] == "chat.postMessage"]
        assert payload["text"].splitlines() == render_text(row).splitlines()
        assert any(len(line) > WRAP_WIDTH for line in payload["text"].splitlines())


class TestDrainExcludesInternalAudience(TestCase):
    """An INTERNAL row (an agent tool-lack self-report) never joins the owner DM batch."""

    def test_internal_only_backlog_drains_nothing(self) -> None:
        DeferredQuestion.record(
            "This session lacks any shell/write tool to run record_candidate.",
        )
        with patch("teatree.core.notify_question_drains.notify_user_outcome") as notify:
            delivered, total = drain_deferred_questions()
        # The INTERNAL row is filtered before any egress — the egress is never called.
        notify.assert_not_called()
        assert (delivered, total) == (0, 0)

    def test_owner_row_drains_but_internal_peer_is_excluded(self) -> None:
        owner = DeferredQuestion.record(
            "Should I merge the widget change?", card=owner_card(OwnerDecision.IRREVERSIBLE, "CI is green")
        )
        DeferredQuestion.record(
            "I run shell-denied and cannot file the issue.",
        )
        with patch("teatree.core.notify_question_drains.notify_user_outcome", return_value=DELIVERED) as notify:
            delivered, total = drain_deferred_questions()
        # Exactly one egress — the owner row — and the total counts only it.
        assert notify.call_count == 1
        assert owner.question in notify.call_args.args[0]
        assert (delivered, total) == (1, 1)


class TestDrainAdvancesPastAlreadyDeliveredRows(TestCase):
    """The per-call cap must bound NEW deliveries, never re-select a delivered head (#4064).

    ``pending()`` is oldest-first and the slice was taken straight off it, so the same
    three oldest rows filled the window on every call, deduped to no-ops, and every row
    behind them was unreachable — on a tick, on an away->present transition, or on a
    manual resurface. The module's own comment promised the opposite ("the remainder is
    re-read on the next tick"), which is the behaviour these pin.
    """

    def _delivered(self, row: DeferredQuestion) -> None:
        """Stand in for an earlier drain that already DM'd *row*."""
        BotPing.objects.create(
            idempotency_key=f"resurface-deferred-question:{row.stable_notify_ref}",
            kind=BotPing.Kind.QUESTION,
            status=BotPing.Status.SENT,
            text="already sent",
        )

    def test_a_delivered_head_does_not_block_the_rest_of_the_backlog(self) -> None:
        rows = [DeferredQuestion.record(f"Q{i}?", **OWNER_DECISION) for i in range(5)]
        for row in rows[:3]:
            self._delivered(row)

        with patch("teatree.core.notify_question_drains.notify_user_outcome", return_value=DELIVERED) as notify:
            delivered, _total = drain_deferred_questions()

        posted = " ".join(str(call.args[0]) for call in notify.call_args_list)
        assert "Q3" in posted, "the window must advance past the delivered head"
        assert "Q4" in posted
        assert "Q0" not in posted, "an already-delivered row must not re-occupy the cap"
        assert delivered == 2

    def test_total_reports_the_backlog_not_the_capped_slice(self) -> None:
        for i in range(5):
            DeferredQuestion.record(f"Q{i}?", **OWNER_DECISION)

        with patch("teatree.core.notify_question_drains.notify_user_outcome", return_value=DELIVERED):
            _delivered, total = drain_deferred_questions()

        assert total == 5, "reporting the slice as the denominator hides the backlog"


class TestOnlyASentPingCountsAsDelivered(TestCase):
    """A FAILED or NOOP ping is a delivery that did not land, so it must not skip the row.

    `notify_user` treats only SENT as already-delivered and re-delivers FAILED / NOOP, so a
    status-blind read-back skips such a question permanently — worse than the head-blocking it
    replaces, where a transient failure self-healed on the next call.
    """

    def _ping(self, row: DeferredQuestion, status: str) -> None:
        BotPing.objects.create(
            idempotency_key=f"resurface-deferred-question:{row.stable_notify_ref}",
            kind=BotPing.Kind.QUESTION,
            status=status,
            text="attempted",
        )

    def test_a_failed_ping_does_not_mark_the_question_delivered(self) -> None:
        row = DeferredQuestion.record("Should I merge the widget change?", **OWNER_DECISION)
        self._ping(row, BotPing.Status.FAILED)

        with patch("teatree.core.notify_question_drains.notify_user_outcome", return_value=DELIVERED) as notify:
            delivered, total = drain_deferred_questions()

        assert delivered == 1
        assert total == 1
        assert row.question in str(notify.call_args.args[0])

    def test_a_noop_ping_does_not_mark_the_question_delivered(self) -> None:
        row = DeferredQuestion.record("Pick a rollout?", **OWNER_DECISION)
        self._ping(row, BotPing.Status.NOOP)

        with patch("teatree.core.notify_question_drains.notify_user_outcome", return_value=DELIVERED) as notify:
            delivered, _total = drain_deferred_questions()

        assert delivered == 1
        assert row.question in str(notify.call_args.args[0])


class TestARowTheSendPathWillNotRedeliverFreesItsCapSlot(TestCase):
    """A ping whose next claim stands down must be skipped by the read-back too (#4064).

    ``BotPing.claim_delivery`` returns ``IN_FLIGHT`` — no DM — for SENT_UNVERIFIED, EXPIRED,
    LOGGED and a FRESH SENDING. A read-back scoped to SENT leaves such a row in the selection
    window, where it is neither skipped by the selection nor re-delivered by the send, so it
    permanently occupies one of the three cap slots and the drain stays head-blocked. The
    predicate is "will the send actually re-deliver this?", not "is it SENT?".
    """

    def _ping(self, row: DeferredQuestion, status: str, *, age: timedelta = timedelta(0)) -> None:
        BotPing.objects.create(
            idempotency_key=f"resurface-deferred-question:{row.stable_notify_ref}",
            kind=BotPing.Kind.QUESTION,
            status=status,
            text="attempted",
            posted_at=timezone.now() - age,
        )

    def test_a_stood_down_head_does_not_re_occupy_a_cap_slot(self) -> None:
        for status in (BotPing.Status.SENT_UNVERIFIED, BotPing.Status.EXPIRED, BotPing.Status.SENDING):
            # ``str(status)``: xdist serialises subTest kwargs across the worker channel and
            # cannot dump a TextChoices member, which fails the node under `-n auto` only.
            with self.subTest(status=str(status)):
                DeferredQuestion.objects.all().delete()
                BotPing.objects.all().delete()
                rows = [DeferredQuestion.record(f"Q{i}?", **OWNER_DECISION) for i in range(5)]
                self._ping(rows[0], status)

                with patch("teatree.core.notify_question_drains.notify_user_outcome", return_value=DELIVERED) as notify:
                    delivered, total = drain_deferred_questions()

                posted = " ".join(str(call.args[0]) for call in notify.call_args_list)
                assert "Q0" not in posted, "a row the send stands down on must not hold a cap slot"
                assert [q for q in ("Q1", "Q2", "Q3") if q in posted] == ["Q1", "Q2", "Q3"]
                assert (delivered, total) == (3, 5)

    def test_a_stale_sending_claim_stays_redeliverable(self) -> None:
        row = DeferredQuestion.record("Should I merge the widget change?", **OWNER_DECISION)
        self._ping(row, BotPing.Status.SENDING, age=BotPing.SENDING_STALE_AFTER + timedelta(seconds=1))

        with patch("teatree.core.notify_question_drains.notify_user_outcome", return_value=DELIVERED) as notify:
            delivered, total = drain_deferred_questions()

        # A crashed claim is recoverable — claim_delivery replaces it and the DM goes out.
        assert (delivered, total) == (1, 1)
        assert row.question in str(notify.call_args.args[0])

"""The answer-cycle wake must not fire on the bot's own posts (#4707).

The bot's ``chat.postMessage`` output returns through Socket Mode as a plain
``message`` event carrying the bot's own ``user`` / ``bot_id`` — Slack stamps
``subtype=bot_message`` only on some post shapes, so the receiver's subtype drop
does not catch it. Waking the answer cycle on that event let the cycle feed on
its own replies: measured at 2,391 ``chat.postMessage`` in 24h, a wake every ~2s,
and 466 ``conversations.replies`` polls of ONE thread in two hours.

``filter_self_messages`` already refuses to ANSWER such a row, but it runs in the
scanner — downstream of the wake — so every self-post still bought a full cycle
and two Slack reads. These tests pin the guard at the trigger, where the loop
gain actually is.
"""

from teatree.backends.slack.receiver import _wake_is_warranted
from teatree.backends.slack.self_identity import OwnSlackIdentity

_OWN = OwnSlackIdentity(user_id="U0BOT", bot_id="B0BOT")


class TestSelfAuthoredEventsRaiseNoWake:
    def test_a_post_carrying_the_bot_user_id_is_not_woken_on(self) -> None:
        assert _wake_is_warranted({"user": "U0BOT", "ts": "1.0"}, _OWN, "acme") is False

    def test_a_post_carrying_the_bot_id_is_not_woken_on(self) -> None:
        # auth.test may return only one of the two ids, and Slack stamps the
        # other field on the post — so either match alone has to be enough.
        assert _wake_is_warranted({"bot_id": "B0BOT", "ts": "1.0"}, _OWN, "acme") is False

    def test_a_genuine_user_message_still_wakes(self) -> None:
        # AV: the negative control. Without it the guard above is satisfiable by
        # a function that refuses every event, which would silently disable the
        # whole event-driven answer path rather than fixing the loop.
        assert _wake_is_warranted({"user": "U0HUMAN", "ts": "1.0"}, _OWN, "acme") is True


class TestUnresolvedIdentityFailsClosed:
    def test_no_identity_means_no_wake(self) -> None:
        # A wake we cannot prove is not self-authored is the failure being fixed;
        # the cadence chain still drains the queue, so the cost is latency only.
        assert _wake_is_warranted({"user": "U0HUMAN", "ts": "1.0"}, None, "acme") is False

    def test_an_empty_identity_cannot_vouch_for_anything(self) -> None:
        # Both ids blank is NOT None, and is_self_authored matches nothing against
        # it — so every event, the bot's own included, would read as foreign. The
        # guard has to fail closed on `is_resolvable`, not merely on `is None`.
        blank = OwnSlackIdentity(user_id="", bot_id="")
        assert blank.is_resolvable is False
        assert _wake_is_warranted({"user": "U0BOT", "ts": "1.0"}, blank, "acme") is False

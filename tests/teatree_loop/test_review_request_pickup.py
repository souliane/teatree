"""Auto-pickup of review requests via Slack mention (#1295 capability B).

When the broadcast scanner sees ``<@user_slack_id>`` in a message text
that also carries an open MR URL, it emits a ``review_request_in_slack``
signal whose payload carries the ``reviewer_username``. The dispatcher
routes this to the mechanical ``assign_gitlab_reviewer`` handler, which
calls the code host's ``assign_reviewer`` API.
"""

from dataclasses import dataclass, field

from django.test import TestCase

from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict

MR_OPEN = "https://gitlab.example.com/team/proj/-/merge_requests/501"
CHANNEL = "C0AAA"
TS = "1779990002.000002"
USER_SLACK_ID = "U12345678"
USERNAME = "souliane"


@dataclass
class FakeMessaging:
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        del since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        del since
        return []

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        del since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        del channel, ts
        return {}

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        del channel, text, thread_ts
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        del channel, ts, text
        return {}

    def open_dm(self, user_id: str) -> str:
        del user_id
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/{channel}/p{ts.replace('.', '')}"

    def resolve_user_id(self, handle: str) -> str:
        del handle
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True}


class ReviewRequestPickupTests(TestCase):
    def test_slack_mention_emits_review_request_in_slack_signal(self) -> None:
        messaging = FakeMessaging()
        text = f"<@{USER_SLACK_ID}> can you review {MR_OPEN}?"
        messages = {CHANNEL: [{"text": text, "ts": TS}]}

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages.get(channel, []))

        def classifier(urls):
            return [MrState(url=url, merged=False, approved=False) for url in urls]

        scanner = SlackBroadcastsScanner(
            backend=messaging,
            channels=[CHANNEL],
            fetch_channel_history=fetch,
            classify_mrs=classifier,
            overlay="test",
            user_slack_id=USER_SLACK_ID,
            reviewer_username=USERNAME,
        )
        signals = scanner.scan()

        pickup_signals = [s for s in signals if s.kind == "review_request_in_slack"]
        assert len(pickup_signals) == 1
        assert pickup_signals[0].payload["reviewer_username"] == USERNAME
        assert pickup_signals[0].payload["url"] == MR_OPEN

    def test_mention_without_target_user_does_not_emit_pickup_signal(self) -> None:
        # Different user mentioned — the scanner must not pick this up.
        messaging = FakeMessaging()
        text = f"<@U99999999> please review {MR_OPEN}"
        messages = {CHANNEL: [{"text": text, "ts": TS}]}

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages.get(channel, []))

        def classifier(urls):
            return [MrState(url=url, merged=False, approved=False) for url in urls]

        scanner = SlackBroadcastsScanner(
            backend=messaging,
            channels=[CHANNEL],
            fetch_channel_history=fetch,
            classify_mrs=classifier,
            overlay="test",
            user_slack_id=USER_SLACK_ID,
            reviewer_username=USERNAME,
        )
        signals = scanner.scan()
        assert not any(s.kind == "review_request_in_slack" for s in signals)

    def test_mention_on_own_authored_mr_does_not_emit_pickup_signal(self) -> None:
        # Even when the user is @-mentioned on a broadcast, an MR authored BY the
        # user must NEVER produce a reviewer-assignment pickup — you do not assign
        # a reviewer on your own MR (sibling of the _apply_classification own-author
        # skip). Guards against the regression where reviewers were assigned on the
        # user's own GitLab MRs.
        messaging = FakeMessaging()
        text = f"<@{USER_SLACK_ID}> can you review {MR_OPEN}?"
        messages = {CHANNEL: [{"text": text, "ts": TS}]}

        def fetch(*, channel: str) -> list[RawAPIDict]:
            return list(messages.get(channel, []))

        def classifier(urls):
            # Every open MR in the broadcast is authored by the current user.
            return [MrState(url=url, merged=False, approved=False, author_username=USERNAME) for url in urls]

        scanner = SlackBroadcastsScanner(
            backend=messaging,
            channels=[CHANNEL],
            fetch_channel_history=fetch,
            classify_mrs=classifier,
            overlay="test",
            user_slack_id=USER_SLACK_ID,
            reviewer_username=USERNAME,
            current_gitlab_username=USERNAME,
        )
        signals = scanner.scan()
        assert not any(s.kind == "review_request_in_slack" for s in signals)

    def test_dispatcher_routes_pickup_to_assign_gitlab_reviewer(self) -> None:
        signal = ScanSignal(
            kind="review_request_in_slack",
            summary="Review request via Slack mention",
            payload={
                "url": MR_OPEN,
                "reviewer_username": USERNAME,
                "channel": CHANNEL,
                "ts": TS,
            },
        )
        actions = dispatch([signal])
        # One mechanical action — the assign handler.
        assert any(a.kind == "mechanical" and a.zone == "assign_gitlab_reviewer" for a in actions), actions

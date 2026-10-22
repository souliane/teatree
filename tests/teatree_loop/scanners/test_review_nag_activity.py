import datetime as dt
from typing import cast

import pytest

from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.review_nag_activity import thread_activity


class TestThreadActivity:
    @pytest.mark.parametrize(
        ("reactions", "requester_slack_id"),
        [
            ([{"name": "eyes", "count": 1}], "UOWNER"),
            ([{"name": "eyes", "users": []}], "UOWNER"),
            ([{"name": "eyes", "users": {"UOWNER": 1}}], "UOWNER"),
            ([{"name": "eyes", "users": ["UOWNER"], "count": 3}], "UOWNER"),
            ("eyes", "UOWNER"),
            (1, "UOWNER"),
            (["eyes"], "UOWNER"),
            ([{"name": "eyes", "users": ["UOWNER"]}], ""),
            ([{"name": "eyes", "users": [""]}], ""),
            ([{"name": "eyes", "users": [42]}], "UOWNER"),
            (
                [
                    {"name": "eyes", "users": ["UOWNER"], "count": 1},
                    {"name": "eyes", "users": ["UPEER"], "count": 1},
                ],
                "UOWNER",
            ),
        ],
        ids=(
            "users-missing",
            "users-empty",
            "users-dict",
            "users-truncated",
            "reactions-not-list",
            "reactions-non-iterable",
            "reaction-not-dict",
            "requester-unresolved",
            "requester-unresolved-empty-user",
            "non-string-user-control",
            "mixed-reactions",
        ),
    )
    def test_unprovable_reaction_counts_as_activity(self, reactions: object, requester_slack_id: str) -> None:
        now = dt.datetime(2026, 10, 18, tzinfo=dt.UTC)
        post = ReviewRequestPost(slack_thread_ts="1000", created_at=now)

        activity = thread_activity(
            post,
            [{"ts": "2000", "reactions": reactions}],
            now,
            requester_slack_id=requester_slack_id,
        )

        assert activity == now

    def test_requester_only_reactions_fall_through_to_reply_timestamps(self) -> None:
        now = dt.datetime(2026, 10, 18, tzinfo=dt.UTC)
        post = ReviewRequestPost(slack_thread_ts="1000", created_at=now)
        replies = [
            {"ts": "1000", "reactions": [{"name": "eyes", "users": ["UOWNER"], "count": 1}]},
            {
                "ts": "2000",
                "reactions": [
                    {"name": "eyes", "users": ["UOWNER"], "count": 1},
                    {"name": "white_check_mark", "users": ["UOWNER"], "count": 1},
                    {"name": "thumbsup", "users": ["UOWNER"]},
                ],
            },
        ]

        activity = thread_activity(post, replies, now, requester_slack_id="UOWNER")

        assert activity == dt.datetime.fromtimestamp(2000, tz=dt.UTC)

    def test_unreadable_messages_and_timestamps_fall_back_to_creation(self) -> None:
        created_at = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
        post = ReviewRequestPost(slack_thread_ts="bad", created_at=created_at)
        replies = cast("list[dict[str, object]]", ["unreadable", {"ts": 3}, {"ts": "bad"}])

        activity = thread_activity(post, replies, created_at, requester_slack_id="UOWNER")

        assert activity == created_at

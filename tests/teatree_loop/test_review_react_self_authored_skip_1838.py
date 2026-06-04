"""The merge :merge: reaction must never land on the user's OWN MR (#1838).

The loop reacts ``:merge:`` on a review-request's Slack message once its
MR merges (#1797) so reviewers see at a glance which requests landed. That
reaction is a *colleague* signal: it must NEVER fire on a review-request
the user posted for their *own* MR. The recurring bug was the bot reacting
to the user's own merge-request review-request message.

The fix gates :func:`react_merge_on_post` on the MR author resolved from
the code host and matched against the user's forge identities (the same
notion of "self" the review-candidate skip-conditions use). This module
proves the gate across the full matrix:

    author   in {user-self, colleague}
    state    in {merged, open}
    reacted  in {already-reacted, not-reacted}

The bot reacts EXACTLY on (colleague, merged, not-reacted) - never on a
self-authored MR, never on an open MR, never twice.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest
from django.utils import timezone

from teatree.backends.protocols import PrOpenState
from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.review_request_merge_react import (
    MERGE_REACTION_EMOJI,
    ReviewRequestMergeReactScanner,
    react_merge_on_post,
)
from teatree.types import RawAPIDict
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate


@pytest.fixture(autouse=True)
def _gate_off(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


_USER_LOGIN = "souliane"
_COLLEAGUE_LOGIN = "a-colleague"
_MR_URL = "https://github.com/o/r/pull/7"
_CHANNEL = "C0AM3TENTLK"
_THREAD_TS = "1780473408.767019"

_SELF = "self"
_COLLEAGUE = "colleague"
_AUTHOR_LOGIN = {_SELF: _USER_LOGIN, _COLLEAGUE: _COLLEAGUE_LOGIN}


@dataclass
class _RecordingSlack:
    """In-memory MessagingBackend recording every ``react_routed`` call."""

    reactions: list[dict[str, Any]] = field(default_factory=list)

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append({"channel": channel, "ts": ts, "emoji": emoji})
        return {"ok": True}


@dataclass
class _AuthoredHost:
    """In-memory ``CodeHostBackend`` with a fixed open-state and author."""

    open_state: PrOpenState
    author: str
    user: str = _USER_LOGIN
    raise_on_author: Exception | None = None
    raise_on_current_user: Exception | None = None

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        _ = pr_url
        return self.open_state

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        if self.raise_on_author is not None:
            raise self.raise_on_author
        return self.author

    def current_user(self) -> str:
        if self.raise_on_current_user is not None:
            raise self.raise_on_current_user
        return self.user


def _seed(*, reacted: bool) -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(
        mr_url=_MR_URL,
        slack_channel_id=_CHANNEL,
        slack_thread_ts=_THREAD_TS,
        created_at=timezone.now(),
        last_nag_step=0,
        done_at=timezone.now() if reacted else None,
    )


@pytest.mark.django_db
class TestSelfAuthoredReactSkipMatrix:
    """author x state x already-reacted: react only on (colleague, merged, not-reacted)."""

    @pytest.mark.parametrize("author", [_SELF, _COLLEAGUE])
    @pytest.mark.parametrize("state", [PrOpenState.MERGED, PrOpenState.OPEN])
    @pytest.mark.parametrize("reacted", [False, True])
    def test_react_only_on_colleague_merged_unreacted(
        self,
        author: str,
        state: PrOpenState,
        reacted: bool,  # noqa: FBT001 — parametrized matrix dimension, not a flag arg.
    ) -> None:
        _seed(reacted=reacted)
        slack = _RecordingSlack()
        host = _AuthoredHost(open_state=state, author=_AUTHOR_LOGIN[author])
        scanner = ReviewRequestMergeReactScanner(
            messaging=slack,
            host=host,
            identities=(_USER_LOGIN,),
        )

        scanner.scan()

        should_react = author == _COLLEAGUE and state is PrOpenState.MERGED and not reacted
        expected = [{"channel": _CHANNEL, "ts": _THREAD_TS, "emoji": MERGE_REACTION_EMOJI}] if should_react else []
        assert slack.reactions == expected, (author, state, reacted)

    def test_self_authored_merge_never_reacts_and_closes_row(self) -> None:
        post = _seed(reacted=False)
        slack = _RecordingSlack()
        host = _AuthoredHost(open_state=PrOpenState.MERGED, author=_USER_LOGIN)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host, identities=(_USER_LOGIN,))

        signals = scanner.scan()

        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is not None
        assert [s.kind for s in signals] == ["review_request_merge_react.self_authored"]

    def test_self_authored_via_alias_identity_never_reacts(self) -> None:
        post = _seed(reacted=False)
        slack = _RecordingSlack()
        host = _AuthoredHost(open_state=PrOpenState.MERGED, author="adrien.cossa", user="")
        scanner = ReviewRequestMergeReactScanner(
            messaging=slack,
            host=host,
            identities=(_USER_LOGIN, "adrien.cossa"),
        )

        scanner.scan()

        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is not None

    def test_self_authored_via_current_user_only_never_reacts(self) -> None:
        post = _seed(reacted=False)
        slack = _RecordingSlack()
        host = _AuthoredHost(open_state=PrOpenState.MERGED, author=_USER_LOGIN, user=_USER_LOGIN)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host, identities=())

        scanner.scan()

        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is not None

    def test_colleague_merge_reacts_exactly_once_across_two_scans(self) -> None:
        _seed(reacted=False)
        slack = _RecordingSlack()
        host = _AuthoredHost(open_state=PrOpenState.MERGED, author=_COLLEAGUE_LOGIN)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host, identities=(_USER_LOGIN,))

        scanner.scan()
        scanner.scan()

        assert slack.reactions == [{"channel": _CHANNEL, "ts": _THREAD_TS, "emoji": MERGE_REACTION_EMOJI}]

    def test_unresolved_author_fails_closed_when_identity_known(self) -> None:
        post = _seed(reacted=False)
        slack = _RecordingSlack()
        host = _AuthoredHost(open_state=PrOpenState.MERGED, author="", user=_USER_LOGIN)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host, identities=(_USER_LOGIN,))

        scanner.scan()

        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is not None

    def test_author_lookup_raising_fails_closed_and_skips(self) -> None:
        post = _seed(reacted=False)
        slack = _RecordingSlack()
        host = _AuthoredHost(
            open_state=PrOpenState.MERGED,
            author="",
            user=_USER_LOGIN,
            raise_on_author=RuntimeError("github 500"),
        )
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host, identities=(_USER_LOGIN,))

        scanner.scan()

        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is not None

    def test_current_user_raising_with_no_aliases_reacts_for_colleague(self) -> None:
        _seed(reacted=False)
        slack = _RecordingSlack()
        host = _AuthoredHost(
            open_state=PrOpenState.MERGED,
            author=_COLLEAGUE_LOGIN,
            raise_on_current_user=RuntimeError("auth_test failed"),
        )
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host, identities=())

        scanner.scan()

        assert slack.reactions == [{"channel": _CHANNEL, "ts": _THREAD_TS, "emoji": MERGE_REACTION_EMOJI}]

    def test_react_merge_on_post_without_thread_ts_is_a_noop(self) -> None:
        post = ReviewRequestPost.objects.create(
            mr_url=_MR_URL,
            slack_channel_id=_CHANNEL,
            slack_thread_ts="",
            created_at=timezone.now(),
            last_nag_step=0,
        )
        slack = _RecordingSlack()
        host = _AuthoredHost(open_state=PrOpenState.MERGED, author=_COLLEAGUE_LOGIN)

        result = react_merge_on_post(post, slack, host=host, identities=(_USER_LOGIN,))

        assert result is None
        assert slack.reactions == []

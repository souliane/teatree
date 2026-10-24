"""The review-DONE reaction goes on a colleague's merge request, never on the operator's own.

Driven through ``emit_review_done_reactions`` with the ledger row real and only the forge and
the Slack transport replaced. The colleague case is the control: a fix that reacts on nothing
passes every other case here.
"""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.models import ConfigSetting, OutboundClaim, ReviewRequestPost
from teatree.loop.review_done_reactions import emit_review_done_reactions
from teatree.types import RawAPIDict
from tests._forge_author import AuthorForge, reaction_forge
from tests._send_gate import allow_slack_channels
from tests.teatree_core._on_behalf_gate_helpers import posture_permits_cm

_SLUG = "team/project"
_PR_ID = 7567
_MR = f"https://gitlab.example.com/{_SLUG}/-/merge_requests/{_PR_ID}"
_CHANNEL = "C0REVIEW"
_TS = "1779990001.000001"


@dataclass
class _Messaging:
    user_id: str = "U0DEMOUSER1"
    routed: list[tuple[str, str, str]] = field(default_factory=list)

    def react_routed(self, *, channel: str, ts: str, emoji: str, **_kwargs: Any) -> RawAPIDict:
        self.routed.append((channel, ts, emoji))
        return {"ok": True}

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = (channel, ts)
        return {}


class TestTheReactionFollowsTheAuthor(TestCase):
    def setUp(self) -> None:
        self.enterContext(posture_permits_cm())
        allow_slack_channels(_CHANNEL)
        ConfigSetting.objects.set_value("user_identity_aliases", ["alice"])
        ReviewRequestPost.objects.create(mr_url=_MR, slack_channel_id=_CHANNEL, slack_thread_ts=_TS)
        self.messaging = _Messaging()

    def _emit(self, forge: AuthorForge | None) -> list[str]:
        with reaction_forge(forge):
            return emit_review_done_reactions(slug=_SLUG, pr_id=_PR_ID, emojis=("eyes",), messaging=self.messaging)

    def _assert_nothing_reacted(self) -> None:
        assert self.messaging.routed == []
        assert OutboundClaim.objects.filter(kind=OutboundClaim.Kind.SLACK_REACTION).count() == 0

    def test_a_colleagues_merge_request_gets_the_reaction(self) -> None:
        assert self._emit(AuthorForge(author="mallory")) == ["eyes"]
        assert self.messaging.routed == [(_CHANNEL, _TS, "eyes")]
        assert OutboundClaim.objects.filter(kind=OutboundClaim.Kind.SLACK_REACTION).count() == 1

    def test_the_operators_own_merge_request_gets_none(self) -> None:
        assert self._emit(AuthorForge(author="alice")) == []
        self._assert_nothing_reacted()

    def test_a_case_only_difference_in_the_login_is_still_the_operator(self) -> None:
        assert self._emit(AuthorForge(author="ALICE")) == []
        self._assert_nothing_reacted()

    def test_an_author_nobody_can_read_gets_none(self) -> None:
        unreadable = {
            "the forge raised": AuthorForge(error=RuntimeError("forge down")),
            "the forge answered an empty author": AuthorForge(author=""),
            "no code host at all": None,
        }
        for case, forge in unreadable.items():
            with self.subTest(case):
                assert self._emit(forge) == []
                self._assert_nothing_reacted()

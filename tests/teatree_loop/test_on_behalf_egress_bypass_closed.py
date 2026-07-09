"""The 4 loop colleague-Slack egress sites honour the on-behalf gate (#960/#1750).

Each site used to call the routed Slack primitive directly with no gate —
the away-mode incident (a 👀 / ✅ placed on a colleague's MR while the user
was away under ``ask``). Now each routes through ``OnBehalfSlackEgress``,
so under ``ask`` with no recorded approval the colleague-surface egress
BLOCKS: the primitive is never called and the site's idempotency claim is
released / a ``.gated`` signal is emitted. A recorded ``OnBehalfApproval``
makes the same egress FIRE exactly once (the gate is satisfiable, not a
kill-switch).
"""

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import BotPing, ConfigSetting, OnBehalfApproval, OutboundClaim, ReviewRequestPost
from teatree.loop.review_claim import emit_review_done_reactions
from teatree.loop.scanners.review_nag import _post_thread_nag
from teatree.loop.scanners.review_request_merge_react import react_merge_on_post
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_DM_CHANNEL = "D_SELF"
_USER_ID = "U_OPERATOR"
_COLLEAGUE = "C_REVIEW"
_TS = "1700000000.001"
_MR = "https://github.com/o/r/pull/1"
_APPROVER = "U-OPERATOR"


@dataclass
class _RouteAwareFake:
    """Route-aware fake (#1750 ``route_token``) recording routed calls + DM sinks."""

    dm_channel_id: str = _DM_CHANNEL
    user_id: str = _USER_ID
    routed_response: RawAPIDict = field(default_factory=lambda: {"ok": True})
    react_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    post_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)
    post_message_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def _is_self_dm(self, channel: str) -> bool:
        return bool(channel) and channel in {self.dm_channel_id, self.user_id}

    def route_token(self, channel: str) -> str:
        return "xoxb-bot" if self._is_self_dm(channel) else "xoxp-user"

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_routed_calls.append((channel, ts, emoji))
        return dict(self.routed_response)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.post_routed_calls.append((channel, text, thread_ts))
        return dict(self.routed_response)

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.post_message_calls.append((channel, text, thread_ts))
        return {"ok": True}

    def open_dm(self, user_id: str) -> str:
        return _DM_CHANNEL

    def resolve_user_id(self, handle: str) -> str:
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return "https://slack.example/p1"


@dataclass
class _Host:
    state: PrOpenState = PrOpenState.MERGED
    user: str = ""

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        return self.state

    def current_user(self) -> str:
        return self.user

    def get_pr_author(self, *, pr_url: str) -> str:
        return "a-colleague"


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    ConfigSetting.objects.set_value("slack_user_id", _USER_ID)
    ConfigSetting.objects.set_value("on_behalf_post_mode", mode)
    ConfigSetting.objects.set_value("review_nag_enabled", value=True)
    monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda _o=None: _RouteAwareFake())


def _seed(**overrides: Any) -> ReviewRequestPost:
    spec: dict[str, Any] = {
        "mr_url": _MR,
        "slack_channel_id": _COLLEAGUE,
        "slack_thread_ts": _TS,
        "created_at": timezone.now() - dt.timedelta(days=2),
        "last_nag_step": 0,
        "done_at": None,
    }
    spec.update(overrides)
    return ReviewRequestPost.objects.create(**spec)


class TestMergeReactBypassClosed(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_blocks_and_releases_claim_under_ask(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, "ask")
        post = _seed()
        fake = _RouteAwareFake()
        signal = react_merge_on_post(post, fake, host=_Host(), identities=())
        assert fake.react_routed_calls == []
        post.refresh_from_db()
        assert post.done_at is None
        assert signal is not None
        assert signal.kind == "review_request_merge_react.gated"

    def test_fires_once_and_audits_with_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, "ask")
        OnBehalfApproval.record(target=_MR, action="merge_reaction", approver_id=_APPROVER)
        post = _seed()
        fake = _RouteAwareFake()
        react_merge_on_post(post, fake, host=_Host(), identities=())
        assert fake.react_routed_calls == [(_COLLEAGUE, _TS, "merge")]
        assert BotPing.objects.filter(idempotency_key=f"on_behalf_post:{_MR}:merge_reaction").exists()


class TestReviewDoneReactionBypassClosed(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_no_reaction_no_claim_under_ask(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, "ask")
        _seed()
        fake = _RouteAwareFake()
        posted = emit_review_done_reactions(slug="o/r", pr_id=1, emojis=["eyes", "white_check_mark"], messaging=fake)
        assert posted == []
        assert fake.react_routed_calls == []
        assert OutboundClaim.objects.count() == 0

    def test_fires_and_claims_with_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, "ask")
        OnBehalfApproval.record(target=_MR, action="review_done_reaction:eyes", approver_id=_APPROVER)
        _seed()
        fake = _RouteAwareFake()
        posted = emit_review_done_reactions(slug="o/r", pr_id=1, emojis=["eyes"], messaging=fake)
        assert posted == ["eyes"]
        assert fake.react_routed_calls == [(_COLLEAGUE, _TS, "eyes")]
        assert OutboundClaim.objects.count() == 1


class TestBroadcastReactionBypassClosed(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def _scanner(self, fake: _RouteAwareFake) -> SlackBroadcastsScanner:
        message = {"ts": _TS, "text": f"please review {_MR}", "channel": _COLLEAGUE}
        return SlackBroadcastsScanner(
            backend=fake,
            channels=[_COLLEAGUE],
            fetch_channel_history=lambda *, channel: [message],
            classify_mrs=lambda urls: [MrState(url=u, merged=True, approved=True) for u in urls],
        )

    def test_no_reaction_under_ask(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, "ask")
        fake = _RouteAwareFake()
        self._scanner(fake).scan()
        assert fake.react_routed_calls == []
        assert fake.react_calls == []

    def test_reacts_routed_with_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, "ask")
        OnBehalfApproval.record(
            target=_MR,
            action="broadcast_outcome_reaction:white_check_mark",
            approver_id=_APPROVER,
        )
        fake = _RouteAwareFake()
        self._scanner(fake).scan()
        assert fake.react_routed_calls == [(_COLLEAGUE, _TS, "white_check_mark")]
        assert fake.react_calls == []


class TestNagPostBypassClosed(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_no_post_and_release_step_under_ask(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, "ask")
        post = _seed()
        fake = _RouteAwareFake()
        signal = _post_thread_nag(post, fake, target_step=1)
        assert fake.post_message_calls == []
        assert fake.post_routed_calls == []
        post.refresh_from_db()
        assert post.last_nag_step == 0
        assert signal is not None
        assert signal.kind == "review_nag.gated"

    def test_posts_with_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, "ask")
        OnBehalfApproval.record(target=_MR, action="review_nag_post", approver_id=_APPROVER)
        post = _seed()
        fake = _RouteAwareFake()
        _post_thread_nag(post, fake, target_step=1)
        assert fake.post_routed_calls == [(_COLLEAGUE, fake.post_routed_calls[0][1], _TS)]
        post.refresh_from_db()
        assert post.last_nag_step == 1

"""Race-safe review-request dedup guard against LIVE Slack messages (#1084).

The guard reads the target channel's recent history *and* takes an
atomic DB claim before a review-request post. Every fake here stops at
the ``conversations.history`` httpx boundary (pattern mirrored from
``tests/teatree_backends/test_slack.py``) — no live Slack call, no
network. The DB is the real Django test DB so the atomic-claim race is
exercised end to end.
"""

import datetime as dt
from typing import TYPE_CHECKING, Self
from unittest.mock import patch

import httpx
import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.backends.slack import client as slack_client
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.gates.review_request_guard import (
    GuardDecision,
    GuardOptions,
    GuardTarget,
    peek_should_post_review_request,
    reconcile_out_of_band,
    resolve_guard_target,
    should_post_review_request,
)
from teatree.core.models import PullRequest, ReviewRequestPost, Ticket

if TYPE_CHECKING:
    from tests.teatree_core.conftest import CommandOverlay

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"
_CHANNEL_ID = "C0DEMOCHAN1"
_CHANNEL_NAME = "the-review-team"
_BOT_AUTHOR = "B_AGENT"
_HUMAN_AUTHOR = "U_HUMAN"


def _ts_now() -> str:
    return f"{timezone.now().timestamp():.6f}"


class FakeClient:
    """Fake httpx.Client returning configurable conversations.history pages."""

    def __init__(
        self,
        *,
        pages: list[dict] | None = None,
        replies: dict | None = None,
        headers: dict[str, str] | None = None,
        raises: BaseException | None = None,
        **_kw: object,
    ) -> None:
        self.pages = pages or []
        self._page_idx = 0
        self.replies = replies
        self.headers = headers or {}
        self._raises = raises
        self.get_calls: list[dict[str, object]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.get_calls.append({"url": url, "headers": dict(self.headers), **kwargs})
        if self._raises is not None:
            raise self._raises
        if "auth.test" in url:
            return httpx.Response(
                200,
                json={"ok": True, "url": "https://team.slack.com/"},
                request=httpx.Request("GET", url),
            )
        if "conversations.history" in url:
            page = self.pages[self._page_idx] if self._page_idx < len(self.pages) else {"ok": False}
            self._page_idx += 1
            return httpx.Response(200, json=page, request=httpx.Request("GET", url))
        if "conversations.replies" in url:
            payload = self.replies if self.replies is not None else {"ok": False, "error": "thread_not_found"}
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))


def _bind(fake: FakeClient, kw: dict) -> FakeClient:
    fake.headers = kw.get("headers", fake.headers)
    return fake


class TestPriorAgentPostSuppresses(TestCase):
    """(a) A prior agent post already in channel history → SUPPRESS + permalink."""

    def test_prior_post_in_history_suppresses(self) -> None:
        page = {
            "ok": True,
            "messages": [
                {"text": f"feat: thing {_MR_URL}", "ts": _ts_now(), "bot_id": _BOT_AUTHOR},
            ],
            "has_more": False,
        }
        fake = FakeClient(pages=[page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "suppress"
        assert decision.permalink.startswith("https://team.slack.com/archives/")
        assert decision.author == _BOT_AUTHOR


class TestUserManualPostSuppresses(TestCase):
    """(b) A user's manual post (DIFFERENT author) → SUPPRESS, author == human.

    A naive "only suppress my own posts" implementation returns POST here
    — caught by asserting suppression on a non-bot author.
    """

    def test_user_manual_post_suppresses_with_human_author(self) -> None:
        page = {
            "ok": True,
            "messages": [
                {"text": f"please review {_MR_URL}", "ts": _ts_now(), "user": _HUMAN_AUTHOR},
            ],
            "has_more": False,
        }
        fake = FakeClient(pages=[page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "suppress"
        assert decision.author == _HUMAN_AUTHOR


class TestRaceAtomicClaim(TestCase):
    """(c) Race: call 1 history empty → POST; call 2 finds URL → SUPPRESS.

    The atomic DB claim from call 1 (get_or_create created=False on the
    second invocation) independently yields SUPPRESS. Exactly one
    effective POST across the two invocations against one test DB.
    """

    def test_two_invocations_yield_exactly_one_post(self) -> None:
        empty_page = {"ok": True, "messages": [], "has_more": False}
        fake1 = FakeClient(pages=[empty_page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake1, kw))
            first = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )

        # Second invocation: history now shows the just-posted message
        # (a concurrent actor) AND the DB claim already exists.
        page2 = {
            "ok": True,
            "messages": [{"text": f"feat {_MR_URL}", "ts": _ts_now(), "bot_id": _BOT_AUTHOR}],
            "has_more": False,
        }
        fake2 = FakeClient(pages=[page2])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake2, kw))
            second = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )

        assert first.action == "post"
        assert second.action == "suppress"
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 1

    def test_db_claim_alone_suppresses_even_if_history_empty(self) -> None:
        """The atomic claim is independent of the live read.

        Once call 1 has claimed the row, a second caller whose live read
        is *still empty* (the concurrent post not yet visible) must still
        SUPPRESS — the get_or_create created=False is the race backstop.
        """
        empty_page = {"ok": True, "messages": [], "has_more": False}
        fake1 = FakeClient(pages=[empty_page])
        fake2 = FakeClient(pages=[{"ok": True, "messages": [], "has_more": False}])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake1, kw))
            first = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake2, kw))
            second = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert first.action == "post"
        assert second.action == "suppress"
        assert second.reason == "already_claimed"
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 1


class TestFailSafeOnReadError(TestCase):
    """(d) httpx error/timeout → SUPPRESS reason=read_failed_failsafe, bounded."""

    def test_timeout_suppresses_failsafe(self) -> None:
        fake = FakeClient(raises=httpx.TimeoutException("slow"))
        start = timezone.now()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
                options=GuardOptions(read_timeout=2.0),
            )
        elapsed = (timezone.now() - start).total_seconds()
        assert decision.action == "suppress"
        assert decision.reason == "read_failed_failsafe"
        # No post recorded; obligation stays open for a later tick.
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0
        # Bounded — no unbounded retry loop.
        assert elapsed < 10.0

    def test_http_error_suppresses_failsafe(self) -> None:
        fake = FakeClient(raises=httpx.HTTPError("boom"))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "suppress"
        assert decision.reason == "read_failed_failsafe"

    def test_api_not_ok_suppresses_failsafe(self) -> None:
        """A non-exception API ok=false read also fails safe to SUPPRESS."""
        fake = FakeClient(pages=[{"ok": False, "error": "channel_not_found"}])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "suppress"
        assert decision.reason == "read_failed_failsafe"
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0

    def test_non_numeric_ts_is_excluded_from_window(self) -> None:
        """A message whose ts is non-numeric is treated as out-of-window."""
        page = {
            "ok": True,
            "messages": [{"text": f"review {_MR_URL}", "ts": "not-a-float", "user": _HUMAN_AUTHOR}],
            "has_more": False,
        }
        fake = FakeClient(pages=[page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "post"


class TestConnectTokenIsReadToken(TestCase):
    """(e) The guard reads history with the token it is given (read==post).

    The caller resolves the Connect⇒xoxp decision via
    slack_token_policy.channel_token and hands the resulting token to the
    guard; the guard must read with exactly that token. Asserted by
    inspecting the captured Authorization header.
    """

    def test_guard_reads_with_supplied_xoxp_token(self) -> None:
        page = {"ok": True, "messages": [], "has_more": False}
        fake = FakeClient(pages=[page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxp-user-connect"),
            )
        history_calls = [c for c in fake.get_calls if "conversations.history" in str(c["url"])]
        assert history_calls
        for call in history_calls:
            headers = call["headers"]
            assert isinstance(headers, dict)
            assert headers["Authorization"] == "Bearer xoxp-user-connect"


class TestReconciliationOnOutOfBandPost(TestCase):
    """A detected out-of-band post reconciles ReviewRequestPost + PullRequest.

    done_at is set so ReviewNagScanner stops nagging; the PR transitions
    OPEN → REVIEW_REQUESTED with the discovered permalink as slack_url.
    The loop Task lifecycle is NOT touched (no Task row created/mutated).
    """

    def test_out_of_band_post_marks_done_and_transitions_pr(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        pr = PullRequest.objects.create(
            ticket=ticket,
            url=_MR_URL,
            repo="org/repo",
            iid="385",
            state=PullRequest.State.OPEN,
        )
        page = {
            "ok": True,
            "messages": [{"text": f"review {_MR_URL}", "ts": _ts_now(), "user": _HUMAN_AUTHOR}],
            "has_more": False,
        }
        fake = FakeClient(pages=[page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )

        assert decision.action == "suppress"
        post = ReviewRequestPost.objects.get(mr_url=_MR_URL)
        assert post.done_at is not None
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.REVIEW_REQUESTED
        assert pr.slack_url == decision.permalink

    def test_recency_window_excludes_old_post(self) -> None:
        """A post older than the recency window does NOT suppress."""
        old_ts = f"{(timezone.now() - dt.timedelta(days=30)).timestamp():.6f}"
        # conversations.history with `oldest` would not return this server
        # side; the fake returns it anyway so the guard's own window
        # filter is what must exclude it.
        page = {
            "ok": True,
            "messages": [{"text": f"review {_MR_URL}", "ts": old_ts, "bot_id": _BOT_AUTHOR}],
            "has_more": False,
        }
        fake = FakeClient(pages=[page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
                options=GuardOptions(recency_window=dt.timedelta(hours=24)),
            )
        assert decision.action == "post"


class TestGuardDecisionShouldPost:
    def test_should_post_true_only_for_post_action(self) -> None:
        assert GuardDecision(action="post").should_post is True
        assert GuardDecision(action="suppress").should_post is False


def _bare_overlay() -> "CommandOverlay":
    from teatree.core.overlay import OverlayConfig  # noqa: PLC0415
    from tests.teatree_core.conftest import CommandOverlay  # noqa: PLC0415

    overlay = CommandOverlay()
    # Per-instance config so we never mutate the class-level default
    # shared across the suite (see test_followup_discover_mrs).
    overlay.config = OverlayConfig()
    return overlay


def _overlay_with_channel() -> "CommandOverlay":
    overlay = _bare_overlay()
    overlay.config.get_review_channel = lambda: (_CHANNEL_NAME, _CHANNEL_ID)  # type: ignore[method-assign]
    overlay.config.get_slack_token = lambda: "xoxb-sync"  # type: ignore[method-assign]
    return overlay


class TestResolveGuardTarget(TestCase):
    def test_returns_none_when_no_overlay(self) -> None:
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

        with patch(
            "teatree.core.overlay_loader.get_overlay",
            side_effect=ImproperlyConfigured("none"),
        ):
            assert resolve_guard_target() is None

    def test_returns_none_when_no_review_channel(self) -> None:
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": _bare_overlay()},
        ):
            assert resolve_guard_target() is None

    def test_uses_sync_token_when_messaging_not_bot(self) -> None:
        overlay = _overlay_with_channel()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": overlay}),
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=None),
        ):
            target = resolve_guard_target()
        assert target is not None
        assert target.token == "xoxb-sync"
        assert target.channel_id == _CHANNEL_ID

    def test_uses_resolved_channel_token_for_slack_bot(self) -> None:
        overlay = _overlay_with_channel()
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": overlay}),
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
            patch.object(backend, "resolve_channel_token", return_value="xoxp-connect") as rct,
        ):
            target = resolve_guard_target()
        assert target is not None
        assert target.token == "xoxp-connect"
        rct.assert_called_once_with(_CHANNEL_ID)

    def test_connect_guard_token_is_xoxp_when_conversations_info_flaky(self) -> None:
        """Guard read-token == post-token even when ``conversations.info`` fails (#1110).

        The #1084 guard reads channel history with the exact token an
        outbound post would use. On a Slack-Connect review channel whose
        ``conversations.info`` probe is flaky (``ok:false``), the
        pre-#1110 policy resolved the guard token to the bot ``xoxb`` —
        a token the Connect channel rejects, so the live dedup read
        always saw an empty history and never suppressed a duplicate
        review-request post. #1110: an unconfirmable Connect channel
        resolves the guard (a WRITE-class read-as-the-post) to the user
        ``xoxp`` token. RED on main: ``target.token == "xoxb-bot"``.

        The real ``resolve_channel_token`` / ``_channel_token`` run; only
        the ``httpx`` boundary is faked so ``conversations.info`` fails.
        """
        from teatree.backends.slack import http as slack_http  # noqa: PLC0415

        overlay = _overlay_with_channel()
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        def fake_get(url: str, **_kwargs: object) -> httpx.Response:
            return httpx.Response(
                200,
                json={"ok": False, "error": "ratelimited"},
                request=httpx.Request("GET", url),
            )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": overlay}),
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend),
            patch.object(slack_http.httpx, "get", fake_get),
        ):
            target = resolve_guard_target()

        assert target is not None
        assert target.token == "xoxp-user"

    def test_returns_none_when_no_token(self) -> None:
        overlay = _bare_overlay()
        overlay.config.get_review_channel = lambda: (_CHANNEL_NAME, _CHANNEL_ID)  # type: ignore[method-assign]
        overlay.config.get_slack_token = lambda: ""  # type: ignore[method-assign]
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": overlay}),
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=None),
        ):
            assert resolve_guard_target() is None

    def test_explicit_channel_id_skips_overlay_channel_lookup(self) -> None:
        overlay = _overlay_with_channel()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": overlay}),
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=None),
        ):
            target = resolve_guard_target(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME)
        assert target is not None
        assert target.channel_id == _CHANNEL_ID


class TestReconcileOutOfBand(TestCase):
    def test_returns_empty_when_read_fails(self) -> None:
        fake = FakeClient(raises=httpx.HTTPError("down"))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            permalink = reconcile_out_of_band(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert permalink == ""

    def test_returns_empty_when_api_not_ok(self) -> None:
        fake = FakeClient(pages=[{"ok": False}])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            permalink = reconcile_out_of_band(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert permalink == ""

    def test_returns_empty_when_nothing_in_window(self) -> None:
        fake = FakeClient(pages=[{"ok": True, "messages": [], "has_more": False}])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            permalink = reconcile_out_of_band(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert permalink == ""

    def test_reconciles_and_returns_permalink(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        pr = PullRequest.objects.create(
            ticket=ticket,
            url=_MR_URL,
            repo="org/repo",
            iid="385",
            state=PullRequest.State.OPEN,
        )
        page = {
            "ok": True,
            "messages": [{"text": f"review {_MR_URL}", "ts": _ts_now(), "user": _HUMAN_AUTHOR}],
            "has_more": False,
        }
        fake = FakeClient(pages=[page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            permalink = reconcile_out_of_band(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert permalink.startswith("https://team.slack.com/archives/")
        post = ReviewRequestPost.objects.get(mr_url=_MR_URL)
        assert post.done_at is not None
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.REVIEW_REQUESTED

    def test_old_window_post_excluded(self) -> None:
        old_ts = f"{(timezone.now() - dt.timedelta(days=40)).timestamp():.6f}"
        fake = FakeClient(pages=[{"ok": True, "messages": [{"text": _MR_URL, "ts": old_ts}], "has_more": False}])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            permalink = reconcile_out_of_band(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert permalink == ""


class TestReconcileIdempotent(TestCase):
    """A second reconcile of an already-done row / non-OPEN PR is a no-op."""

    def test_reconcile_is_idempotent(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        PullRequest.objects.create(
            ticket=ticket,
            url=_MR_URL,
            repo="org/repo",
            iid="385",
            state=PullRequest.State.APPROVED,
        )
        ReviewRequestPost.objects.create(
            mr_url=_MR_URL,
            slack_channel_id=_CHANNEL_ID,
            slack_thread_ts="1.0",
            done_at=timezone.now(),
        )
        page = {
            "ok": True,
            "messages": [{"text": f"x {_MR_URL}", "ts": _ts_now(), "user": _HUMAN_AUTHOR}],
            "has_more": False,
        }
        fake = FakeClient(pages=[page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            permalink = reconcile_out_of_band(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert permalink != ""
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 1


class TestStaleOrphanReclaim(TestCase):
    """A stale unposted orphan must not suppress an authoritative POST (#1103).

    ``review_request_check`` (pre-#1103) left a durable claim row with
    ``done_at=None`` and ``slack_thread_ts=''``. Such an orphan older
    than ``_CLAIM_RACE_WINDOW`` is NOT a concurrent dup — the live scan
    is the authority that nothing was posted, so the orphan is reclaimed.
    A *recent* orphan (< window) is still a genuine race → SUPPRESS.
    """

    def test_stale_orphan_does_not_suppress(self) -> None:
        stale_at = timezone.now() - dt.timedelta(minutes=5)
        ReviewRequestPost.objects.create(
            mr_url=_MR_URL,
            slack_channel_id="",
            slack_thread_ts="",
            done_at=None,
            created_at=stale_at,
        )
        fake = FakeClient(pages=[{"ok": True, "messages": [], "has_more": False}])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "post"
        post = ReviewRequestPost.objects.get(mr_url=_MR_URL)
        assert post.created_at > stale_at
        assert post.slack_channel_id == _CHANNEL_ID
        assert post.done_at is None
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 1

    def test_recent_unposted_claim_suppresses(self) -> None:
        ReviewRequestPost.objects.create(
            mr_url=_MR_URL,
            slack_channel_id=_CHANNEL_ID,
            slack_thread_ts="",
            done_at=None,
            created_at=timezone.now(),
        )
        fake = FakeClient(pages=[{"ok": True, "messages": [], "has_more": False}])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "suppress"
        assert decision.reason == "already_claimed"

    def test_posted_row_with_live_thread_beyond_window_suppresses(self) -> None:
        """A posted row whose thread is still LIVE (verified) suppresses (#1084 follow-up).

        The channel-history window read is empty (the post is older than the
        window), so the DB alone used to decide. Now the exact thread is
        live-verified: present ⇒ SUPPRESS ``already_claimed``.
        """
        ReviewRequestPost.objects.create(
            mr_url=_MR_URL,
            slack_channel_id=_CHANNEL_ID,
            slack_thread_ts="1700000000.000100",
            done_at=None,
            created_at=timezone.now() - dt.timedelta(days=40),
        )
        fake = FakeClient(
            pages=[{"ok": True, "messages": [], "has_more": False}],
            replies={"ok": True, "messages": [{"ts": "1700000000.000100", "text": f"review {_MR_URL}"}]},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "suppress"
        assert decision.reason == "already_claimed"

    def test_posted_row_with_deleted_thread_reclaims_and_posts(self) -> None:
        """A posted row whose thread is GONE (deleted) is reclaimed → POST.

        Live Slack, not the DB row, is the authority: an empty
        ``conversations.replies`` means the message is gone, so the row is
        atomically reclaimed and the guard POSTs a fresh request.
        """
        ReviewRequestPost.objects.create(
            mr_url=_MR_URL,
            slack_channel_id=_CHANNEL_ID,
            slack_thread_ts="1700000000.000100",
            done_at=timezone.now(),
            created_at=timezone.now() - dt.timedelta(days=40),
        )
        fake = FakeClient(
            pages=[{"ok": True, "messages": [], "has_more": False}],
            replies={"ok": True, "messages": []},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "post"
        post = ReviewRequestPost.objects.get(mr_url=_MR_URL)
        assert post.slack_thread_ts == ""
        assert post.done_at is None
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 1

    def test_posted_row_thread_read_failure_suppresses_failsafe(self) -> None:
        """ANY failure reading the posted row's thread fails safe to SUPPRESS."""
        ReviewRequestPost.objects.create(
            mr_url=_MR_URL,
            slack_channel_id=_CHANNEL_ID,
            slack_thread_ts="1700000000.000100",
            done_at=None,
            created_at=timezone.now() - dt.timedelta(days=40),
        )
        fake = FakeClient(
            pages=[{"ok": True, "messages": [], "has_more": False}],
            replies={"ok": False, "error": "ratelimited"},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "suppress"
        assert decision.reason == "read_failed_failsafe"


class TestPeekPostedRowVerification(TestCase):
    """``check`` (peek) gets the SAME live posted-row verification, but writes nothing (#1084 follow-up)."""

    def test_peek_live_thread_suppresses_without_mutating(self) -> None:
        ReviewRequestPost.objects.create(
            mr_url=_MR_URL,
            slack_channel_id=_CHANNEL_ID,
            slack_thread_ts="1700000000.000100",
            done_at=None,
            created_at=timezone.now() - dt.timedelta(days=40),
        )
        fake = FakeClient(
            pages=[{"ok": True, "messages": [], "has_more": False}],
            replies={"ok": True, "messages": [{"ts": "1700000000.000100", "text": _MR_URL}]},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = peek_should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "suppress"
        post = ReviewRequestPost.objects.get(mr_url=_MR_URL)
        assert post.done_at is None  # peek never mutates

    def test_peek_deleted_thread_reports_post_without_reclaiming(self) -> None:
        ReviewRequestPost.objects.create(
            mr_url=_MR_URL,
            slack_channel_id=_CHANNEL_ID,
            slack_thread_ts="1700000000.000100",
            done_at=timezone.now(),
            created_at=timezone.now() - dt.timedelta(days=40),
        )
        fake = FakeClient(
            pages=[{"ok": True, "messages": [], "has_more": False}],
            replies={"ok": True, "messages": []},
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = peek_should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "post"
        post = ReviewRequestPost.objects.get(mr_url=_MR_URL)
        assert post.slack_thread_ts == "1700000000.000100"  # unchanged — peek writes nothing


class TestPeekTakesNoClaim(TestCase):
    """``peek_should_post_review_request`` never persists a row (#1103)."""

    def test_peek_clean_scan_posts_without_claim(self) -> None:
        fake = FakeClient(pages=[{"ok": True, "messages": [], "has_more": False}])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = peek_should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "post"
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0

    def test_peek_passes_through_terminal_suppress(self) -> None:
        page = {
            "ok": True,
            "messages": [{"text": f"review {_MR_URL}", "ts": _ts_now(), "user": _HUMAN_AUTHOR}],
            "has_more": False,
        }
        fake = FakeClient(pages=[page])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            decision = peek_should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert decision.action == "suppress"
        assert decision.reason == "already_posted"


class TestNoLoopTaskTouched(TestCase):
    """#1086 coupling guard: the dedup guard never touches loop Task rows.

    #1084 must reconcile via ReviewRequestPost + PullRequest only — the
    reviewing-Task lifecycle is owned by souliane/teatree#1086.
    """

    def test_reconcile_creates_no_task_rows(self) -> None:
        from teatree.core.models import Task  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="t3-teatree")
        PullRequest.objects.create(ticket=ticket, url=_MR_URL, repo="org/repo", iid="385", state=PullRequest.State.OPEN)
        page = {
            "ok": True,
            "messages": [{"text": f"x {_MR_URL}", "ts": _ts_now(), "user": _HUMAN_AUTHOR}],
            "has_more": False,
        }
        fake = FakeClient(pages=[page])
        before = Task.objects.count()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
            should_post_review_request(
                mr_url=_MR_URL,
                target=GuardTarget(channel_id=_CHANNEL_ID, channel_name=_CHANNEL_NAME, token="xoxb-bot"),
            )
        assert Task.objects.count() == before

"""Tests for teatree.backends.slack.reactions."""

from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest

from teatree.backends.slack import reactions as slack_reactions
from teatree.backends.slack.reactions import (
    _iter_pr_permalinks,
    add_approval_reaction,
    add_reaction,
    add_reactions_for_transition,
    parse_permalink,
)
from teatree.core.overlay import DEFAULT_TRANSITION_EMOJIS


class TestParsePermalink:
    def test_extracts_channel_and_inserts_ts_dot(self) -> None:
        assert parse_permalink("https://team.slack.com/archives/C0123/p1700000000000100") == (
            "C0123",
            "1700000000.000100",
        )

    def test_returns_none_on_empty(self) -> None:
        assert parse_permalink("") is None

    def test_returns_none_when_no_archive_segment(self) -> None:
        assert parse_permalink("https://team.slack.com/messages/C0123/p1700000000000100") is None

    def test_returns_none_when_ts_too_short(self) -> None:
        assert parse_permalink("https://team.slack.com/archives/C0123/p12345") is None

    def test_handles_thread_reply_with_query_string(self) -> None:
        permalink = (
            "https://team.slack.com/archives/C0DEMOCHAN1/p1774852840536479?thread_ts=1774618737.744799&cid=C0DEMOCHAN1"
        )
        assert parse_permalink(permalink) == ("C0DEMOCHAN1", "1774852840.536479")


@dataclass
class _FakePost:
    responses: list[httpx.Response]
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


class TestAddReaction:
    def test_returns_false_when_any_arg_missing(self) -> None:
        assert add_reaction("", "C1", "1.0", "tada") is False
        assert add_reaction("t", "", "1.0", "tada") is False
        assert add_reaction("t", "C1", "", "tada") is False
        assert add_reaction("t", "C1", "1.0", "") is False

    def test_posts_and_returns_true_on_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        post = _FakePost(responses=[httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "x"))])
        monkeypatch.setattr(slack_reactions.httpx, "post", post)

        assert add_reaction("xoxb", "C1", "1700.000100", "tada") is True
        assert post.calls[0]["url"] == "https://slack.com/api/reactions.add"
        assert post.calls[0]["data"] == {"channel": "C1", "timestamp": "1700.000100", "name": "tada"}

    def test_already_reacted_counts_as_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        post = _FakePost(
            responses=[
                httpx.Response(200, json={"ok": False, "error": "already_reacted"}, request=httpx.Request("POST", "x")),
            ],
        )
        monkeypatch.setattr(slack_reactions.httpx, "post", post)
        assert add_reaction("xoxb", "C1", "1.0", "tada") is True

    def test_other_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#1281: any Slack ``ok:false`` raises :class:`SlackReactionError`.

        Pre-#1281 the helper returned ``False`` on every non-``already_reacted``
        error — that let callers silently substitute a
        ``chat.postMessage(text=":emoji:")`` thread reply, which the BINDING
        memory ``feedback_react_not_emoji_thread_comment`` forbids. Raising
        loudly at the helper boundary forecloses the silent swallow.
        """
        from teatree.backends.slack.react_errors import SlackReactionError  # noqa: PLC0415

        post = _FakePost(
            responses=[
                httpx.Response(
                    200,
                    json={"ok": False, "error": "channel_not_found"},
                    request=httpx.Request("POST", "x"),
                ),
            ],
        )
        monkeypatch.setattr(slack_reactions.httpx, "post", post)
        with pytest.raises(SlackReactionError) as exc_info:
            add_reaction("xoxb", "C1", "1.0", "tada")
        assert exc_info.value.error_code == "channel_not_found"

    def test_http_error_swallowed_and_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: object, **_kw: object) -> httpx.Response:
            msg = "boom"
            raise httpx.ConnectError(msg)

        monkeypatch.setattr(slack_reactions.httpx, "post", _raise)
        assert add_reaction("xoxb", "C1", "1.0", "tada") is False

    def test_non_2xx_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        post = _FakePost(responses=[httpx.Response(500, request=httpx.Request("POST", "x"))])
        monkeypatch.setattr(slack_reactions.httpx, "post", post)
        assert add_reaction("xoxb", "C1", "1.0", "tada") is False

    def test_non_json_2xx_body_degrades_to_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 2xx body that is not JSON degrades to ``False`` rather than crashing (#1881).

        Slack (or an intercepting proxy) can return a 2xx whose body is HTML or
        empty. ``response.json()`` then raises ``json.JSONDecodeError``; an
        uncaught raise crashes the reaction path instead of degrading it. The
        unparsable 2xx must take the same failure contract as a transport
        error: logged + ``return False``, never propagated.
        """
        post = _FakePost(
            responses=[httpx.Response(200, content=b"<html>not json</html>", request=httpx.Request("POST", "x"))],
        )
        monkeypatch.setattr(slack_reactions.httpx, "post", post)
        assert add_reaction("xoxb", "C1", "1.0", "tada") is False


class TestIterPrPermalinks:
    def test_collects_only_non_empty_string_permalinks(self) -> None:
        ticket = SimpleNamespace(
            extra={
                "prs": {
                    "a": {"review_permalink": "https://team.slack.com/archives/C1/p1700000000000100"},
                    "b": {"review_permalink": ""},
                    "c": {},
                    "d": {"review_permalink": 42},
                    "e": "not-a-dict",
                },
            },
        )
        assert _iter_pr_permalinks(ticket) == ["https://team.slack.com/archives/C1/p1700000000000100"]

    def test_empty_when_no_prs(self) -> None:
        assert _iter_pr_permalinks(SimpleNamespace(extra={})) == []
        assert _iter_pr_permalinks(SimpleNamespace(extra={"prs": "garbage"})) == []
        assert _iter_pr_permalinks(SimpleNamespace(extra=None)) == []


@dataclass
class _StubConfig:
    token: str
    emojis: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TRANSITION_EMOJIS))

    def get_slack_token(self) -> str:
        return self.token

    def get_transition_emojis(self) -> dict[str, str]:
        return self.emojis


@dataclass
class _StubOverlay:
    config: _StubConfig


class TestAddReactionsForTransition:
    def _ticket(
        self,
        permalinks: list[str],
        overlay: str = "",
        role: str = "author",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            extra={"prs": {f"pr-{i}": {"review_permalink": p} for i, p in enumerate(permalinks)}},
            overlay=overlay,
            role=role,
        )

    def test_posts_one_reaction_per_permalink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = _StubOverlay(_StubConfig(token="xoxb"))
        monkeypatch.setattr(
            "teatree.backends.slack.reactions.get_overlay",
            lambda name=None: overlay,
        )
        calls: list[tuple[str, str, str, str]] = []

        def _fake_add(token: str, channel: str, ts: str, emoji: str) -> bool:
            calls.append((token, channel, ts, emoji))
            return True

        monkeypatch.setattr(slack_reactions, "add_reaction", _fake_add)

        ticket = self._ticket(
            [
                "https://team.slack.com/archives/C111/p1700000001000100",
                "https://team.slack.com/archives/C222/p1700000002000200",
            ],
        )
        assert add_reactions_for_transition(ticket, "mark_merged") == 2
        assert calls == [
            ("xoxb", "C111", "1700000001.000100", "tada"),
            ("xoxb", "C222", "1700000002.000200", "tada"),
        ]

    def test_skips_unparseable_permalinks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = _StubOverlay(_StubConfig(token="xoxb"))
        monkeypatch.setattr("teatree.backends.slack.reactions.get_overlay", lambda name=None: overlay)
        monkeypatch.setattr(slack_reactions, "add_reaction", lambda *a, **kw: True)

        ticket = self._ticket(["not-a-permalink", "https://team.slack.com/archives/C1/p1700000000000100"])
        assert add_reactions_for_transition(ticket, "mark_merged") == 1

    def test_no_op_when_transition_unmapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = _StubOverlay(_StubConfig(token="xoxb"))
        monkeypatch.setattr("teatree.backends.slack.reactions.get_overlay", lambda name=None: overlay)
        called = []
        monkeypatch.setattr(slack_reactions, "add_reaction", lambda *a, **kw: called.append(a) or True)

        ticket = self._ticket(["https://team.slack.com/archives/C1/p1700000000000100"])
        assert add_reactions_for_transition(ticket, "unmapped_transition") == 0
        assert called == []

    def test_no_op_when_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = _StubOverlay(_StubConfig(token=""))
        monkeypatch.setattr("teatree.backends.slack.reactions.get_overlay", lambda name=None: overlay)
        called = []
        monkeypatch.setattr(slack_reactions, "add_reaction", lambda *a, **kw: called.append(a) or True)

        ticket = self._ticket(["https://team.slack.com/archives/C1/p1700000000000100"])
        assert add_reactions_for_transition(ticket, "mark_merged") == 0
        assert called == []

    def test_counts_only_successful_reactions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = _StubOverlay(_StubConfig(token="xoxb"))
        monkeypatch.setattr("teatree.backends.slack.reactions.get_overlay", lambda name=None: overlay)
        results = iter([True, False, True])
        monkeypatch.setattr(slack_reactions, "add_reaction", lambda *a, **kw: next(results))

        ticket = self._ticket(
            [
                "https://team.slack.com/archives/C1/p1700000001000100",
                "https://team.slack.com/archives/C2/p1700000002000100",
                "https://team.slack.com/archives/C3/p1700000003000100",
            ],
        )
        assert add_reactions_for_transition(ticket, "mark_merged") == 2

    def test_skips_eyes_on_authored_ticket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Author's own request_review must NOT put :eyes: on the author's PR broadcast.

        The transition ``request_review`` maps to ``eyes`` in the default emoji map,
        signalling "someone is engaging with this PR". When the loop user — the
        ticket's author — transitions their OWN ticket into the reviewing phase,
        posting :eyes: on the author's review-team broadcast looks like the author
        is reviewing their own MR, which inverts the intended signal. Skip these
        reactions when ``ticket.role == "author"`` and the transition emoji is
        :eyes:.
        """
        overlay = _StubOverlay(_StubConfig(token="xoxb"))
        monkeypatch.setattr("teatree.backends.slack.reactions.get_overlay", lambda name=None: overlay)
        calls: list[tuple[str, str, str, str]] = []
        monkeypatch.setattr(
            slack_reactions,
            "add_reaction",
            lambda token, ch, ts, emoji: calls.append((token, ch, ts, emoji)) or True,
        )

        ticket = self._ticket(
            [
                "https://team.slack.com/archives/C111/p1700000001000100",
                "https://team.slack.com/archives/C222/p1700000002000200",
            ],
            role="author",
        )
        assert add_reactions_for_transition(ticket, "request_review") == 0
        assert calls == []

    def test_posts_eyes_on_reviewer_ticket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reviewer role still triggers :eyes: — the author skip does not affect reviewer flows."""
        overlay = _StubOverlay(_StubConfig(token="xoxb"))
        monkeypatch.setattr("teatree.backends.slack.reactions.get_overlay", lambda name=None: overlay)
        calls: list[tuple[str, str, str, str]] = []
        monkeypatch.setattr(
            slack_reactions,
            "add_reaction",
            lambda token, ch, ts, emoji: calls.append((token, ch, ts, emoji)) or True,
        )

        ticket = self._ticket(
            ["https://team.slack.com/archives/C111/p1700000001000100"],
            role="reviewer",
        )
        assert add_reactions_for_transition(ticket, "request_review") == 1
        assert calls == [("xoxb", "C111", "1700000001.000100", "eyes")]

    def test_posts_outcome_emoji_on_authored_ticket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Outcome emojis (tada, white_check_mark) still post on authored tickets — only :eyes: is gated."""
        overlay = _StubOverlay(_StubConfig(token="xoxb"))
        monkeypatch.setattr("teatree.backends.slack.reactions.get_overlay", lambda name=None: overlay)
        calls: list[tuple[str, str, str, str]] = []
        monkeypatch.setattr(
            slack_reactions,
            "add_reaction",
            lambda token, ch, ts, emoji: calls.append((token, ch, ts, emoji)) or True,
        )

        ticket = self._ticket(
            ["https://team.slack.com/archives/C111/p1700000001000100"],
            role="author",
        )
        assert add_reactions_for_transition(ticket, "mark_merged") == 1
        assert calls == [("xoxb", "C111", "1700000001.000100", "tada")]

    def test_overlay_override_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = _StubOverlay(_StubConfig(token="xoxb", emojis={"mark_merged": "rocket"}))
        monkeypatch.setattr("teatree.backends.slack.reactions.get_overlay", lambda name=None: overlay)
        recorded: list[str] = []
        monkeypatch.setattr(slack_reactions, "add_reaction", lambda _t, _c, _ts, emoji: recorded.append(emoji) or True)

        ticket = self._ticket(["https://team.slack.com/archives/C1/p1700000000000100"])
        add_reactions_for_transition(ticket, "mark_merged")
        assert recorded == ["rocket"]


class TestOverlayConfigTransitionEmojis:
    """OverlayConfig.get_transition_emojis merges override onto defaults."""

    def test_returns_defaults_when_unset(self) -> None:
        from teatree.core.overlay import OverlayConfig  # noqa: PLC0415

        config = OverlayConfig()
        emojis = config.get_transition_emojis()
        assert emojis == DEFAULT_TRANSITION_EMOJIS
        # returned dict is a copy — mutating it must not affect future calls
        emojis["mark_merged"] = "poop"
        assert config.get_transition_emojis()["mark_merged"] == DEFAULT_TRANSITION_EMOJIS["mark_merged"]

    def test_override_merges_on_top_of_defaults(self) -> None:
        from teatree.core.overlay import OverlayConfig  # noqa: PLC0415

        config = OverlayConfig()
        config.transition_emojis = {"mark_merged": "rocket", "ship": "ship"}
        merged = config.get_transition_emojis()
        assert merged["mark_merged"] == "rocket"
        assert merged["ship"] == "ship"
        # Default keys still present
        assert merged["rework"] == DEFAULT_TRANSITION_EMOJIS["rework"]
        assert merged["test"] == DEFAULT_TRANSITION_EMOJIS["test"]


class TestAddApprovalReaction:
    """add_approval_reaction posts ✅ on the PR's stored review-request message (#961)."""

    _PERMALINK = "https://team.slack.com/archives/C9/p1700000000000100"

    def _pr(self, *, slack_url: str = _PERMALINK, overlay: str = "") -> SimpleNamespace:
        return SimpleNamespace(slack_url=slack_url, overlay=overlay)

    def test_no_op_when_no_slack_url(self) -> None:
        assert add_approval_reaction(self._pr(slack_url="")) == 0

    def test_no_op_when_permalink_unparseable(self) -> None:
        assert add_approval_reaction(self._pr(slack_url="https://team.slack.com/not-an-archive")) == 0

    def test_no_op_when_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(slack_reactions, "get_overlay", lambda **_kw: _StubOverlay(config=_StubConfig(token="")))
        assert add_approval_reaction(self._pr()) == 0

    def test_posts_white_check_mark_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple[str, str, str, str]] = []
        monkeypatch.setattr(
            slack_reactions, "get_overlay", lambda **_kw: _StubOverlay(config=_StubConfig(token="xoxb"))
        )
        monkeypatch.setattr(
            slack_reactions,
            "add_reaction",
            lambda token, ch, ts, emoji: recorded.append((token, ch, ts, emoji)) or True,
        )
        assert add_approval_reaction(self._pr()) == 1
        assert recorded == [("xoxb", "C9", "1700000000.000100", "white_check_mark")]

    def test_returns_zero_when_reaction_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            slack_reactions, "get_overlay", lambda **_kw: _StubOverlay(config=_StubConfig(token="xoxb"))
        )
        monkeypatch.setattr(slack_reactions, "add_reaction", lambda *_a, **_kw: False)
        assert add_approval_reaction(self._pr()) == 0

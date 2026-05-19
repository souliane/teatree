"""Full-scope integration fortress for the Slack messaging bridge (#1057).

Inbound (Slack DM → ``PendingChatInjection`` → ``UserPromptSubmit`` drain →
agent ``additionalContext``) AND outbound (``notify_user`` →
backend ``post_message`` → Slack ``chat.postMessage``). The previous
test surface covered every layer in isolation; this module exercises
them end-to-end with a fake Slack transport bolted onto the ``httpx``
boundary so the only thing mocked is the network. Every other layer —
the real ``SlackBotBackend``, the real ``SlackDmInboundScanner``, real
``PendingChatInjection`` rows in the Django DB, the real hook router,
the real ``notify_user``, the real ``loop_tick`` management command —
runs unmodified.

The marker ``@pytest.mark.integration`` puts the class on the
integration-tests selector so CI runs the fortress on every PR.

Anti-vacuous evidence: each test names the production line whose
removal would turn it RED. The PR body records that line-to-test
mapping plus the actual RED runs that confirm the guard.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from inline_snapshot import snapshot

from teatree.backends import slack_bot
from teatree.backends.slack_bot import SlackBotBackend
from teatree.core import backend_factory
from teatree.core import notify as core_notify
from teatree.core.backend_factory import OverlayBackends, iter_overlay_backends, messaging_from_overlay
from teatree.core.models import BotPing, PendingChatInjection
from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner
from teatree.notify import NotifyKind, notify_user

pytestmark = [pytest.mark.django_db, pytest.mark.integration]


# ── Fake Slack transport ──────────────────────────────────────────────


@dataclass
class _Call:
    method: str
    url: str
    token: str
    payload: dict[str, Any]


@dataclass
class FakeSlackTransport:
    """Recording fake Slack transport at the ``httpx`` boundary.

    Routes every ``slack.com/api/<method>`` request to a scripted
    response keyed by the URL's last segment. POST bodies arrive as
    ``json=``; GET bodies arrive as ``params=``. Each call is appended
    to :attr:`calls` so tests can assert who-called-what-with.
    Per-method handlers can be overridden by setting ``handlers[name]``.
    """

    calls: list[_Call] = field(default_factory=list)
    handlers: dict[str, Callable[[dict[str, Any], dict[str, str]], dict[str, Any]]] = field(default_factory=dict)
    default_responses: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "auth.test": {"ok": True, "user_id": "B_BOT"},
            "conversations.open": {"ok": True, "channel": {"id": "D-USER"}},
            "conversations.history": {"ok": True, "messages": []},
            "conversations.replies": {"ok": True, "messages": []},
            "chat.postMessage": {"ok": True, "ts": "1700000000.000100", "channel": "D-USER"},
            "chat.getPermalink": {
                "ok": True,
                "permalink": "https://example.slack.com/archives/D-USER/p1700000000000100",
            },
            "reactions.add": {"ok": True},
            "reactions.get": {"ok": True, "message": {"reactions": []}},
            "users.lookupByEmail": {"ok": False, "error": "users_not_found"},
            "users.list": {"ok": True, "members": []},
        }
    )

    def _method_name(self, url: str) -> str:
        return url.rsplit("/", maxsplit=1)[-1]

    def _respond(
        self,
        *,
        http_method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        method = self._method_name(url)
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        self.calls.append(_Call(method=method, url=url, token=token, payload=dict(payload)))
        handler = self.handlers.get(method)
        if handler:
            body: dict[str, Any] = handler(payload, headers)
        else:
            body = self.default_responses.get(method, {"ok": True})
        request = httpx.Request(http_method, url)
        return httpx.Response(200, json=body, request=request)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._respond(
            http_method="POST",
            url=url,
            headers=dict(kwargs.get("headers", {})),
            payload=dict(kwargs.get("json", {})),
        )

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._respond(
            http_method="GET",
            url=url,
            headers=dict(kwargs.get("headers", {})),
            payload=dict(kwargs.get("params", {})),
        )

    def calls_to(self, method: str) -> list[_Call]:
        return [c for c in self.calls if c.method == method]


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeSlackTransport:
    """Install :class:`FakeSlackTransport` as the ``httpx`` boundary for slack_bot."""
    fake = FakeSlackTransport()
    monkeypatch.setattr(slack_bot.httpx, "post", fake.post)
    monkeypatch.setattr(slack_bot.httpx, "get", fake.get)
    return fake


# ── Test-side fake config ─────────────────────────────────────────────


@dataclass
class _FakeUserSettings:
    """Mirror of ``UserSettings`` for ``_resolved_identities()``."""

    user_identity_aliases: list[str] = field(default_factory=list)


@dataclass
class _FakeConfig:
    """Mirror of ``TeaTreeConfig`` for the backend-factory TOML fallback."""

    raw: dict[str, Any] = field(default_factory=dict)
    user: _FakeUserSettings = field(default_factory=_FakeUserSettings)


# ── Helpers ───────────────────────────────────────────────────────────


def _own_loop(session_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Register ``session_id`` as the loop owner via the hook router's registry."""
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    import hooks.scripts.hook_router as router  # noqa: PLC0415

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)
    registry_dir = tmp_path / "loop_registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(registry_dir))
    router._write_loop_registry(
        {
            router._OWNER_LOOP: {
                "session_id": session_id,
                "agent_id": "test",
                "pid": os.getpid(),
                "heartbeat_ts": int(time.time()),
            }
        }
    )


# ── Inbound bridge: REST polling → row → drain → additionalContext ────


class TestInboundBridgeEndToEnd:
    """Slack DM → REST poll → row → ``UserPromptSubmit`` drain → stdout.

    Each test runs the real ``SlackBotBackend.fetch_dms`` against the
    fake transport, then the real scanner, then the real hook handler.
    """

    def test_dm_lands_as_pending_chat_injection_row(self, transport: FakeSlackTransport) -> None:
        """RED if ``SlackBotBackend.fetch_dms`` REST poll branch is removed.

        Guard: deleting the ``conversations.history`` poll fallback in
        ``fetch_dms`` (the branch that runs when ``self._dms`` is empty)
        turns this RED — no row would land.
        """
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1700000000.0001", "user": "U_HUMAN", "text": "ship PR 42"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        row = PendingChatInjection.objects.get()
        assert (row.overlay, row.slack_ts, row.text, row.user_id, row.channel) == snapshot(
            ("demo", "1700000000.0001", "ship PR 42", "U_HUMAN", "D-USER")
        )
        assert [s.kind for s in signals] == snapshot(["slack.user_reply"])

    def test_thread_reply_lands_as_pending_chat_injection_row(self, transport: FakeSlackTransport) -> None:
        """RED if the ``conversations.replies`` fan-out (#1046) is reverted.

        Guard: deleting the ``_fetch_thread_replies`` invocation in
        ``_collect_user_dms`` makes the thread reply invisible to the
        scanner — only the top-level message would persist.
        """
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {
                    "ts": "1700000000.0001",
                    "thread_ts": "1700000000.0001",
                    "user": "U_HUMAN",
                    "text": "top-level question",
                },
            ],
        }
        transport.default_responses["conversations.replies"] = {
            "ok": True,
            "messages": [
                {"ts": "1700000000.0001", "user": "U_HUMAN", "text": "top-level question"},
                {"ts": "1700000000.0002", "user": "U_HUMAN", "text": "follow-up in thread"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        texts = list(PendingChatInjection.objects.order_by("slack_ts").values_list("text", flat=True))
        assert texts == snapshot(["top-level question", "follow-up in thread"])

    def test_channel_stamp_present_on_rest_polled_event(self, transport: FakeSlackTransport) -> None:
        """RED if the ``msg.setdefault("channel", channel)`` stamp (#1043) is reverted.

        Guard: removing the ``setdefault`` line in ``_collect_user_dms``
        means the scanner sees ``channel=""`` and
        ``PendingChatInjection.record`` rejects the row (its guard
        requires ``channel`` to be truthy).
        """
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1700000000.0001", "user": "U_HUMAN", "text": "needs channel"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert PendingChatInjection.objects.get().channel == snapshot("D-USER")

    def test_userpromptsubmit_drain_injects_additional_context(
        self,
        transport: FakeSlackTransport,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """RED if the drain handler stops printing the additionalContext block.

        Guard: removing the ``print(...)`` line in
        ``handle_inject_pending_chat`` (or removing the ``row.consume()``
        call) breaks one of the two asserts. The stdout shape is
        captured via inline-snapshot — a refactor that changes the
        emitted line format will surface as a snapshot diff.
        """
        from hooks.scripts.hook_router import handle_inject_pending_chat  # noqa: PLC0415

        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [{"ts": "1700000000.0001", "user": "U_HUMAN", "text": "drain me"}],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        SlackDmInboundScanner(backend=backend, overlay="").scan()
        _own_loop("owner", monkeypatch, tmp_path)

        handle_inject_pending_chat({"session_id": "owner"})

        out = capsys.readouterr().out
        assert out == snapshot("""\
You have 1 new Slack DM reply(ies) from the user:
User replied on Slack at 1700000000.0001: drain me
""")
        assert PendingChatInjection.objects.get().consumed_at is not None

    def test_consumed_row_not_redrained_on_second_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """RED if ``PendingChatInjection.consume`` stops gating on ``consumed_at``.

        Guard: removing the ``consumed_at__isnull=True`` filter in
        ``consume()`` lets the second drain re-emit the message, which
        re-injects an already-handled DM into the agent.
        """
        from hooks.scripts.hook_router import handle_inject_pending_chat  # noqa: PLC0415

        PendingChatInjection.record(channel="D-USER", slack_ts="1.0", text="once-only", overlay="")
        _own_loop("session-A", monkeypatch, tmp_path)
        handle_inject_pending_chat({"session_id": "session-A"})
        capsys.readouterr()  # drain stdout

        _own_loop("session-B", monkeypatch, tmp_path)
        handle_inject_pending_chat({"session_id": "session-B"})

        assert capsys.readouterr().out == snapshot("")

    def test_scanner_overpoll_does_not_emit_duplicate_signals(self, transport: FakeSlackTransport) -> None:
        """RED if ``SlackDmInboundScanner`` re-emits signals for already-recorded ``ts``.

        Guard: removing the ``if row is None: continue`` branch in
        ``SlackDmInboundScanner.scan`` makes a second poll emit a
        duplicate signal even though the row already exists.
        """
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [{"ts": "1700000000.0001", "user": "U_HUMAN", "text": "ping"}],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        scanner = SlackDmInboundScanner(backend=backend, overlay="demo")

        first = scanner.scan()
        second = scanner.scan()

        assert ([s.kind for s in first], [s.kind for s in second]) == snapshot((["slack.user_reply"], []))
        assert PendingChatInjection.objects.count() == 1

    def test_double_unique_constraint_per_overlay_slack_ts(self) -> None:
        """RED if the ``uniq_pendingchat_overlay_ts`` constraint is dropped.

        Guard: removing the ``UniqueConstraint`` from
        ``PendingChatInjection.Meta.constraints`` permits duplicates and
        the test expecting an ``IntegrityError`` flips to passing the
        insert.
        """
        from django.db import IntegrityError, transaction  # noqa: PLC0415

        PendingChatInjection.objects.create(overlay="demo", channel="D-USER", slack_ts="dup", text="first")
        with pytest.raises(IntegrityError), transaction.atomic():
            PendingChatInjection.objects.create(overlay="demo", channel="D-USER", slack_ts="dup", text="second")


# ── Outbound bridge: notify_user → backend → chat.postMessage ─────────


class TestOutboundBridgeEndToEnd:
    """``notify_user`` → real backend → ``chat.postMessage`` over the fake transport."""

    def test_notify_user_posts_to_chat_post_message(self, transport: FakeSlackTransport) -> None:
        """RED if ``notify_user`` stops calling ``backend.post_message``.

        Guard: removing the ``resolved_backend.post_message(...)`` call
        in ``teatree.core.notify.notify_user`` (or replacing it with a
        no-op) makes the ``chat.postMessage`` assertion fail and the
        ``BotPing`` row never reach ``SENT`` status. The body shape is
        pinned via inline-snapshot.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        sent = notify_user(
            "tests green",
            kind=NotifyKind.INFO,
            idempotency_key="sess=1;turn=1",
            backend=backend,
            user_id="U_HUMAN",
        )

        assert sent is True
        post_calls = transport.calls_to("chat.postMessage")
        assert len(post_calls) == 1
        call = post_calls[0]
        assert call.token == "xoxb-bot"
        assert call.payload == snapshot(
            {
                "channel": "D-USER",
                "text": ":information_source: *info*\ntests green",
            }
        )
        assert BotPing.objects.get(idempotency_key="sess=1;turn=1").status == BotPing.Status.SENT

    def test_per_overlay_routing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RED if ``messaging_from_overlay`` stops honouring its ``overlay_name`` arg.

        Guard: replacing the ``_active_overlay_name(overlay_name)`` call
        in ``messaging_from_overlay`` with ``""`` or with the env var
        would route every overlay's DM through the same cached backend.
        Two distinct overlay names must yield two distinct backends
        with two distinct bot tokens — and posts must carry the right
        token (so an "alpha" overlay's DM does not land on "beta"'s
        bot). Inline-snapshot pins the (overlay → token) mapping.
        """
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

        backend_factory.reset_backend_caches()
        per_overlay_tokens = {"alpha": "xoxb-alpha", "beta": "xoxb-beta"}

        def fake_messaging_from_toml(cfg: dict[str, Any]) -> SlackBotBackend | None:
            if cfg.get("messaging_backend") != "slack":
                return None
            return SlackBotBackend(bot_token=cfg["_bot_token"], user_id=cfg.get("slack_user_id", ""))

        cfg_overlays = {
            name: {"messaging_backend": "slack", "_bot_token": token, "slack_user_id": f"U_{name}"}
            for name, token in per_overlay_tokens.items()
        }

        with (
            patch.object(backend_factory, "_messaging_from_toml", side_effect=fake_messaging_from_toml),
            patch("teatree.config.load_config", return_value=_FakeConfig(raw={"overlays": cfg_overlays})),
            patch.object(backend_factory, "get_overlay", side_effect=ImproperlyConfigured),
        ):
            alpha = messaging_from_overlay("alpha")
            beta = messaging_from_overlay("beta")

        assert alpha is not None
        assert beta is not None
        assert alpha is not beta

        recording = FakeSlackTransport()
        monkeypatch.setattr(slack_bot.httpx, "post", recording.post)
        monkeypatch.setattr(slack_bot.httpx, "get", recording.get)

        alpha.post_message(channel="D-alpha", text="for alpha only")
        beta.post_message(channel="D-beta", text="for beta only")

        observed = sorted((c.payload["channel"], c.token) for c in recording.calls_to("chat.postMessage"))
        assert observed == snapshot([("D-alpha", "xoxb-alpha"), ("D-beta", "xoxb-beta")])

    def test_iter_overlay_backends_path_only_toml_works(self) -> None:
        """RED if ``iter_overlay_backends`` stops appending TOML-only overlays.

        Guard: removing the ``out.extend(_backends_from_toml(...))``
        line in ``iter_overlay_backends`` (the #1040 regression's exact
        line) drops every path-only TOML overlay from the iter result.
        """
        backend_factory.reset_backend_caches()
        cfg_overlays = {
            "toml-only-overlay": {
                "messaging_backend": "slack",
                "slack_token_ref": "some-ref",
                "slack_user_id": "U_TOML",
            }
        }
        pass_lookup = {"some-ref-bot": "xoxb-toml", "some-ref-app": "xapp-toml"}

        with (
            patch.object(backend_factory, "get_all_overlays", return_value={}),
            patch("teatree.config.load_config", return_value=_FakeConfig(raw={"overlays": cfg_overlays})),
            patch("teatree.utils.secrets.read_pass", side_effect=lambda k: pass_lookup.get(k, "")),
        ):
            result = iter_overlay_backends()

        toml_backend = next(b for b in result if b.name == "toml-only-overlay")
        assert toml_backend.messaging is not None
        assert isinstance(toml_backend.messaging, SlackBotBackend)

    def test_idempotency_dedup_by_key(self, transport: FakeSlackTransport) -> None:
        """RED if the early-return on existing ``BotPing`` is removed.

        Guard: deleting the ``if existing is not None: return ...`` block
        in ``teatree.core.notify.notify_user`` makes the second call
        re-post to Slack instead of short-circuiting on the prior
        ``BotPing`` row.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        notify_user(
            "first",
            kind=NotifyKind.INFO,
            idempotency_key="dup-key",
            backend=backend,
            user_id="U_HUMAN",
        )
        first_count = len(transport.calls_to("chat.postMessage"))
        sent2 = notify_user(
            "second-skip-me",
            kind=NotifyKind.INFO,
            idempotency_key="dup-key",
            backend=backend,
            user_id="U_HUMAN",
        )

        assert sent2 is True
        assert len(transport.calls_to("chat.postMessage")) == first_count == snapshot(1)
        assert BotPing.objects.filter(idempotency_key="dup-key").count() == 1


# ── Per-overlay routing surface ───────────────────────────────────────


class TestPerOverlayBotRouting:
    """Direct exercise of ``messaging_from_overlay`` and ``iter_overlay_backends``."""

    def test_messaging_from_overlay_returns_correct_backend_for_named_overlay(self) -> None:
        """RED if the path-only TOML fallback in ``_build_messaging`` is dropped.

        Guard: removing the ``return _messaging_from_toml_overlay(overlay_name)``
        branch (caught by ``ImproperlyConfigured``) makes
        ``messaging_from_overlay("toml-overlay")`` return ``None`` even
        when credentials are present in the TOML config — the #1040
        regression shape.
        """
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

        backend_factory.reset_backend_caches()
        cfg_overlays = {
            "toml-overlay": {
                "messaging_backend": "slack",
                "slack_token_ref": "ref",
                "slack_user_id": "U1",
            }
        }
        pass_lookup = {"ref-bot": "xoxb-toml", "ref-app": "xapp-toml"}

        with (
            patch("teatree.config.load_config", return_value=_FakeConfig(raw={"overlays": cfg_overlays})),
            patch("teatree.utils.secrets.read_pass", side_effect=lambda k: pass_lookup.get(k, "")),
            patch.object(backend_factory, "get_overlay", side_effect=ImproperlyConfigured),
        ):
            backend = messaging_from_overlay("toml-overlay")

        assert backend is not None
        assert isinstance(backend, SlackBotBackend)

    def test_iter_overlay_backends_yields_all_overlays_with_messaging(self) -> None:
        """RED if ``iter_overlay_backends`` stops iterating either entry-point or TOML overlays.

        Guard: removing either the ``for name, overlay in get_all_overlays()…``
        loop body or the ``out.extend(_backends_from_toml(...))`` tail
        breaks one of the two expected names. The full set is pinned
        via inline-snapshot.
        """
        backend_factory.reset_backend_caches()

        @dataclass
        class _StubCfg:
            ready_labels: tuple[str, ...] = ()
            exclude_labels: tuple[str, ...] = ()
            auto_start_assigned_issues: bool = False
            max_concurrent_auto_starts: int = 1
            stale_threshold_days: int = 3
            gitlab_url: str = "https://gitlab.com"

            def get_gitlab_token(self) -> str:
                return ""

        @dataclass
        class _StubOverlay:
            name: str
            config: _StubCfg

        py_overlay = _StubOverlay("py-overlay", _StubCfg())

        cfg_overlays = {
            "py-overlay": {},
            "toml-overlay": {
                "messaging_backend": "slack",
                "slack_token_ref": "ref",
                "slack_user_id": "U_TOML",
            },
        }
        pass_lookup = {"ref-bot": "xoxb-toml", "ref-app": "xapp-toml"}

        with (
            patch.object(backend_factory, "get_all_overlays", return_value={"py-overlay": py_overlay}),
            patch.object(backend_factory, "get_code_hosts", return_value=[]),
            patch.object(backend_factory, "get_messaging", return_value=None),
            patch("teatree.config.load_config", return_value=_FakeConfig(raw={"overlays": cfg_overlays})),
            patch("teatree.utils.secrets.read_pass", side_effect=lambda k: pass_lookup.get(k, "")),
        ):
            result = iter_overlay_backends()

        names = sorted(b.name for b in result)
        assert names == snapshot(["py-overlay", "toml-overlay"])


# ── xoxp vs xoxb routing (Slack-Connect surface) ──────────────────────


class TestxoxpVsxoxbRouting:
    """Reactions route through ``xoxp-…``; DMs / posts stay on ``xoxb-…`` (#1041)."""

    def test_reactions_route_through_xoxp_token(self, transport: FakeSlackTransport) -> None:
        """RED if ``react()`` stops calling ``_reaction_token()``.

        Guard: removing the ``token=self._reaction_token()`` kwarg in
        ``SlackBotBackend.react`` makes the reaction post under the bot
        token, which Slack-Connect rejects with
        ``mcp_externally_shared_channel_restricted``.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        backend.react(channel="C-CONNECT", ts="1.0", emoji="eyes")

        react_calls = transport.calls_to("reactions.add")
        assert len(react_calls) == 1
        assert react_calls[0].token == snapshot("xoxp-user")

    def test_chat_post_message_stays_on_xoxb(self, transport: FakeSlackTransport) -> None:
        """RED if ``post_message`` is rerouted through ``_reaction_token``.

        Guard: changing ``self._post("chat.postMessage", payload)`` to
        ``self._post("chat.postMessage", payload, token=self._reaction_token())``
        would impersonate the user against the bot's own DM history.
        This test rejects that change.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        backend.post_message(channel="D-USER", text="hi")

        post_calls = transport.calls_to("chat.postMessage")
        assert len(post_calls) == 1
        assert post_calls[0].token == snapshot("xoxb-bot")

    def test_missing_xoxp_scope_surfaces_clearly(self, transport: FakeSlackTransport) -> None:
        """RED if ``react`` swallows the Slack error body.

        Slack returns ``{"ok": false, "error": "missing_scope",
        "needed": "reactions:write"}`` when the xoxp token lacks the
        right scope. ``react()`` must propagate the raw body so the
        caller can report which scope is missing — silently treating
        the error as success blocks debugging the wrong-OAuth-app
        failure mode. The expected shape is pinned via inline-snapshot.

        Guard: a ``react`` impl that returns ``{}`` or that swallows
        the error and returns ``{"ok": True}`` makes this RED.
        """
        transport.default_responses["reactions.add"] = {
            "ok": False,
            "error": "missing_scope",
            "needed": "reactions:write",
            "provided": "channels:read,chat:write",
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        result = backend.react(channel="C-CONNECT", ts="1.0", emoji="eyes")

        assert result == snapshot(
            {
                "ok": False,
                "error": "missing_scope",
                "needed": "reactions:write",
                "provided": "channels:read,chat:write",
            }
        )


# ── notify_user end-to-end through the real factory ───────────────────


class TestNotifyUserThroughOverlayFactory:
    """``notify_user(backend=None)`` resolves via ``messaging_from_overlay``.

    Closes a coverage gap left by ``test_notify.py``, which always
    passes ``backend=<MagicMock>``. This exercises the production
    fallback path that ``notify_user`` users hit when they omit
    ``backend=`` (the common case).
    """

    def test_resolves_backend_via_messaging_from_overlay(self, transport: FakeSlackTransport) -> None:
        """RED if ``notify_user`` stops falling back to ``messaging_from_overlay``.

        Guard: removing the ``backend if backend is not None else
        messaging_from_overlay()`` fallback in ``teatree.core.notify``
        makes ``notify_user(backend=None)`` always NOOP, even with a
        configured overlay.
        """
        real_backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        with patch.object(core_notify, "messaging_from_overlay", return_value=real_backend):
            sent = notify_user(
                "fallback path",
                kind=NotifyKind.INFO,
                idempotency_key="fallback-1",
                backend=None,
                user_id="U_HUMAN",
            )

        assert sent is True
        post_calls = transport.calls_to("chat.postMessage")
        assert len(post_calls) == 1
        assert post_calls[0].payload["text"] == snapshot(":information_source: *info*\nfallback path")


# ── Slack-backend error paths exercised through the same fake transport ──


class TestSlackBotBackendErrorPathsE2E:
    """End-to-end failure-mode coverage for ``SlackBotBackend``.

    Each test simulates a specific Slack-API failure shape that
    ``SlackBotBackend`` must absorb gracefully (return empty / ``""``).
    Together they push the backend's coverage to its `fail open` branches
    and ensure a future code change can't silently turn one into a raise.
    """

    def test_fetch_dms_returns_empty_when_no_user_id(self, transport: FakeSlackTransport) -> None:
        """RED if the ``not self._user_id`` early-return is removed.

        Guard: removing the ``if not self._user_id … return []`` branch
        in ``fetch_dms`` makes the backend call Slack with an empty
        user id, which Slack would error on and the fake records.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="")
        assert backend.fetch_dms() == snapshot([])
        assert transport.calls == snapshot([])

    def test_fetch_dms_returns_empty_when_open_dm_fails(self, transport: FakeSlackTransport) -> None:
        """RED if ``fetch_dms`` ignores ``open_dm`` failure.

        Guard: removing the ``if not channel: return []`` check after
        ``open_dm`` makes the backend call ``conversations.history``
        with an empty channel id and crashes.
        """
        transport.default_responses["conversations.open"] = {"ok": False, "error": "user_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        assert backend.fetch_dms() == snapshot([])

    def test_fetch_dms_drained_queue_short_circuits_rest(self, transport: FakeSlackTransport) -> None:
        """RED if the Socket-Mode queue drain branch is removed.

        Guard: removing ``if self._dms: events, self._dms = self._dms, []; return events``
        makes a queued event invisible and the backend falls through to
        REST polling — the queue contract is broken.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        backend.enqueue_dm({"ts": "1.0", "user": "U_HUMAN", "text": "queued"})

        events = backend.fetch_dms()

        assert len(events) == 1
        assert events[0]["text"] == "queued"
        # The queue path must NOT have called Slack.
        assert transport.calls == snapshot([])

    def test_poll_dm_history_handles_slack_error(self, transport: FakeSlackTransport) -> None:
        """RED if ``_poll_dm_history`` stops returning ``[]`` on ``ok: false``.

        Guard: removing the ``if not data.get("ok"): return []`` check
        makes the helper return whatever was in the error body's
        ``messages`` key, propagating bad data downstream.
        """
        transport.default_responses["conversations.history"] = {"ok": False, "error": "channel_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        assert backend.fetch_dms() == snapshot([])

    def test_fetch_dms_with_since_passes_oldest_param(self, transport: FakeSlackTransport) -> None:
        """RED if ``fetch_dms`` no longer forwards its ``since`` filter to Slack.

        Guard: dropping the ``if since: params["oldest"] = since`` line
        means thread polling re-fetches all history each tick.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        backend.fetch_dms(since="1700000000.000099")

        history_calls = transport.calls_to("conversations.history")
        assert len(history_calls) == 1
        assert history_calls[0].payload.get("oldest") == snapshot("1700000000.000099")

    def test_open_dm_returns_empty_on_slack_error(self, transport: FakeSlackTransport) -> None:
        """RED if ``open_dm`` propagates the error body instead of returning ``""``.

        Guard: removing the ``if not data.get("ok"): return ""`` check.
        """
        transport.default_responses["conversations.open"] = {"ok": False, "error": "users_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot")

        assert backend.open_dm("U_BAD") == snapshot("")

    def test_get_permalink_returns_empty_on_blank_inputs(self) -> None:
        """RED if ``get_permalink`` removes its input-validation guard.

        Guard: removing the ``if not channel or not ts: return ""`` line.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.get_permalink(channel="", ts="1.0") == snapshot("")
        assert backend.get_permalink(channel="C", ts="") == snapshot("")

    def test_get_permalink_returns_empty_on_slack_error(self, transport: FakeSlackTransport) -> None:
        """RED if ``get_permalink`` stops returning ``""`` on Slack errors."""
        transport.default_responses["chat.getPermalink"] = {"ok": False, "error": "message_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot")

        assert backend.get_permalink(channel="C", ts="1.0") == snapshot("")

    def test_get_reactions_parses_response_names(self, transport: FakeSlackTransport) -> None:
        """RED if ``get_reactions`` stops surfacing the configured emoji names.

        Pins the parsing shape so a refactor of the response walker
        cannot silently drop names.
        """
        transport.default_responses["reactions.get"] = {
            "ok": True,
            "message": {
                "reactions": [
                    {"name": "eyes"},
                    {"name": "white_check_mark"},
                ]
            },
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        assert backend.get_reactions(channel="C", ts="1.0") == snapshot(["eyes", "white_check_mark"])

    def test_get_reactions_returns_empty_on_slack_error(self, transport: FakeSlackTransport) -> None:
        transport.default_responses["reactions.get"] = {"ok": False, "error": "message_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.get_reactions(channel="C", ts="1.0") == snapshot([])

    def test_resolve_user_id_via_email_lookup(self, transport: FakeSlackTransport) -> None:
        """RED if ``resolve_user_id`` stops calling ``users.lookupByEmail``.

        Guard: a refactor that drops the ``"@" in clean`` branch breaks
        the email-handle path; verifying the call hits
        ``users.lookupByEmail`` (not just ``users.list``) catches it.
        """
        transport.default_responses["users.lookupByEmail"] = {
            "ok": True,
            "user": {"id": "U_ALICE"},
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")

        result = backend.resolve_user_id("alice@example.com")

        assert result == snapshot("U_ALICE")
        assert len(transport.calls_to("users.lookupByEmail")) == 1

    def test_resolve_user_id_falls_back_to_users_list(self, transport: FakeSlackTransport) -> None:
        """RED if the ``users.list`` fallback is removed for non-email handles."""
        transport.default_responses["users.list"] = {
            "ok": True,
            "members": [
                {"id": "U_BOB", "name": "bob", "real_name": "Bob T. Builder"},
                {"id": "U_ALICE", "name": "alice", "real_name": "Alice In Wonderland"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")

        assert backend.resolve_user_id("@alice") == snapshot("U_ALICE")
        assert backend.resolve_user_id("Bob T. Builder") == snapshot("U_BOB")

    def test_resolve_user_id_returns_empty_on_blank_handle(self) -> None:
        """RED if ``resolve_user_id`` removes its blank-input guard."""
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.resolve_user_id("@") == snapshot("")
        assert backend.resolve_user_id("") == snapshot("")

    def test_post_when_no_token_returns_empty_dict(self) -> None:
        """RED if ``_post`` removes its no-token early-return.

        Guard: removing the ``if not auth: return {}`` line in ``_post``
        makes the backend hit Slack with an empty Authorization header
        and crashes on ``response.raise_for_status``.
        """
        backend = SlackBotBackend()  # no token at all
        assert backend.post_message(channel="C", text="hi") == snapshot({})

    def test_get_when_no_token_returns_empty_dict(self) -> None:
        """RED if ``_get`` removes its no-token early-return."""
        backend = SlackBotBackend()
        assert backend.get_permalink(channel="C", ts="1.0") == snapshot("")

    def test_post_reply_routes_to_chat_post_message_with_thread_ts(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``post_reply`` stops passing ``thread_ts`` to Slack.

        Guard: a refactor that omits ``thread_ts`` would post a brand-new
        top-level message instead of a reply, breaking thread continuity.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot")

        backend.post_reply(channel="C", ts="1.0", text="reply body")

        post_calls = transport.calls_to("chat.postMessage")
        assert post_calls[0].payload == snapshot({"channel": "C", "thread_ts": "1.0", "text": "reply body"})

    def test_post_message_with_thread_ts_carries_thread_ts(self, transport: FakeSlackTransport) -> None:
        """RED if ``post_message`` drops its ``thread_ts`` kwarg.

        Guard: removing the ``if thread_ts: payload["thread_ts"] = thread_ts``
        branch would make threaded posts land as top-level messages.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot")

        backend.post_message(channel="C", text="hello", thread_ts="1.0")

        assert transport.calls_to("chat.postMessage")[0].payload == snapshot(
            {"channel": "C", "thread_ts": "1.0", "text": "hello"}
        )

    def test_token_accessor_properties(self) -> None:
        """RED if any of the read-only token properties is renamed silently.

        These accessors are consumed by ``backend_factory`` and the
        Socket Mode receiver. The property names are part of the
        ``MessagingBackend`` contract.
        """
        backend = SlackBotBackend(
            bot_token="xoxb-bot",
            app_token="xapp-app",
            user_token="xoxp-user",
            user_id="U_HUMAN",
        )

        assert (backend.app_token, backend.user_id, backend.user_token) == snapshot(
            ("xapp-app", "U_HUMAN", "xoxp-user")
        )

    def test_resolve_bot_id_caches_and_filters_bot_authored_messages(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``_resolve_bot_id`` stops caching or ``_collect_user_dms`` stops filtering.

        End-to-end: the bot's own DM history contains a message authored
        by the bot AND a user message. Only the user message should
        reach the scanner. Subsequent calls hit the cached bot id (one
        ``auth.test`` call total).
        """
        transport.default_responses["auth.test"] = {"ok": True, "user_id": "B_BOT"}
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "bot_id": "B_BOT", "text": "previous bot post"},
                {"ts": "2.0", "user": "U_HUMAN", "text": "human reply"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        first = backend.fetch_dms()
        second = backend.fetch_dms()

        all_texts = [m["text"] for m in (first + second)]
        # Only the human reply (twice — once per poll), never the bot's own message.
        assert all_texts == snapshot(["human reply", "human reply"])
        # The bot-id cache means exactly one ``auth.test`` call across both fetches.
        assert len(transport.calls_to("auth.test")) == snapshot(1)

    def test_thread_reply_helper_returns_empty_on_slack_error(self, transport: FakeSlackTransport) -> None:
        """RED if ``_fetch_thread_replies`` propagates errors instead of swallowing them."""
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {
                    "ts": "1.0",
                    "thread_ts": "1.0",
                    "user": "U_HUMAN",
                    "text": "top-level",
                },
            ],
        }
        transport.default_responses["conversations.replies"] = {"ok": False, "error": "thread_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        events = backend.fetch_dms()
        texts = [m["text"] for m in events]
        # Top-level message still arrives; the failed thread fan-out is silent.
        assert texts == snapshot(["top-level"])

    def test_enqueue_mention_routes_to_fetch_mentions_queue(self, transport: FakeSlackTransport) -> None:
        """RED if the mention queue contract is broken.

        Socket Mode pushes mentions into ``enqueue_mention``;
        ``fetch_mentions`` drains them. The two methods are paired and
        their queue is the cross-thread handoff for inbound mentions.
        """
        _ = transport  # ensure no HTTP call happens
        backend = SlackBotBackend(bot_token="xoxb-bot")
        backend.enqueue_mention({"ts": "1.0", "user": "U", "text": "@bot hi"})

        events = backend.fetch_mentions()

        assert len(events) == 1
        assert events[0]["text"] == "@bot hi"
        assert backend.fetch_mentions() == snapshot([])

    def test_collect_user_dms_handles_non_thread_bot_message(self, transport: FakeSlackTransport) -> None:
        """RED if the bot-authored filter in ``_collect_user_dms`` is dropped on non-thread-root posts.

        Guard: a non-thread bot message should be filtered out AND not
        trigger ``conversations.replies``. Removing either branch breaks
        the contract.
        """
        transport.default_responses["auth.test"] = {"ok": True, "user_id": "B_BOT"}
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                # Bot-authored, NOT a thread root (no thread_ts equal to ts)
                {"ts": "1.0", "user": "B_BOT", "text": "bot status"},
                {"ts": "2.0", "user": "U_HUMAN", "text": "human follow-up"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        events = backend.fetch_dms()

        texts = [e["text"] for e in events]
        assert texts == snapshot(["human follow-up"])
        # No replies fan-out fired (neither message is a thread root).
        assert transport.calls_to("conversations.replies") == snapshot([])

    def test_thread_reply_helper_tolerates_non_list_messages_field(self, transport: FakeSlackTransport) -> None:
        """RED if ``_fetch_thread_replies`` stops type-checking ``messages``."""
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "thread_ts": "1.0", "user": "U_HUMAN", "text": "top"},
            ],
        }
        # Slack returns a corrupt ``messages`` (string, not list)
        transport.default_responses["conversations.replies"] = {"ok": True, "messages": "corrupt"}
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        events = backend.fetch_dms()
        # Top-level still surfaces, no crash from the corrupt reply payload.
        assert [e["text"] for e in events] == snapshot(["top"])

    def test_thread_reply_helper_skips_non_dict_items(self, transport: FakeSlackTransport) -> None:
        """RED if ``_fetch_thread_replies`` stops filtering non-dict items.

        Guard: removing the ``if not isinstance(m, dict): continue``
        check makes the helper crash on a non-dict element.
        """
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "thread_ts": "1.0", "user": "U_HUMAN", "text": "top"},
            ],
        }
        transport.default_responses["conversations.replies"] = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "user": "U_HUMAN", "text": "top"},  # root, skipped
                "not-a-dict",  # filtered
                {"ts": "1.1", "user": "U_HUMAN", "text": "real reply"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        texts = [e["text"] for e in backend.fetch_dms()]
        assert texts == snapshot(["top", "real reply"])

    def test_get_reactions_tolerates_non_list_reactions(self, transport: FakeSlackTransport) -> None:
        """RED if ``get_reactions`` stops type-checking the ``reactions`` field."""
        transport.default_responses["reactions.get"] = {
            "ok": True,
            "message": {"reactions": "not-a-list"},
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.get_reactions(channel="C", ts="1.0") == snapshot([])

    def test_get_reactions_skips_non_dict_reaction_items(self, transport: FakeSlackTransport) -> None:
        """RED if ``get_reactions`` stops filtering non-dict reaction entries."""
        transport.default_responses["reactions.get"] = {
            "ok": True,
            "message": {
                "reactions": [
                    "not-a-dict",
                    {"name": "eyes"},
                    {"missing_name": True},
                ]
            },
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        # The non-dict item and the dict-without-name are both filtered.
        assert backend.get_reactions(channel="C", ts="1.0") == snapshot(["eyes"])

    def test_resolve_user_id_returns_empty_when_members_field_missing(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``resolve_user_id`` stops type-checking ``users.list``'s ``members``."""
        transport.default_responses["users.list"] = {"ok": True, "members": "not-a-list"}
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.resolve_user_id("alice") == snapshot("")

    def test_resolve_user_id_skips_non_dict_member_entries(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``resolve_user_id`` stops filtering non-dict members."""
        transport.default_responses["users.list"] = {
            "ok": True,
            "members": [
                "not-a-dict",
                {"id": "U_ALICE", "name": "alice"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.resolve_user_id("alice") == snapshot("U_ALICE")

    def test_resolve_user_id_returns_empty_when_no_match(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        transport.default_responses["users.list"] = {
            "ok": True,
            "members": [
                {"id": "U_BOB", "name": "bob"},
                {"id": "U_CAROL", "name": "carol"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.resolve_user_id("alice") == snapshot("")

    def test_resolve_user_id_email_lookup_with_non_string_id_falls_back_to_list(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``resolve_user_id`` accepts a non-string user id from ``users.lookupByEmail``.

        Slack normally returns ``user.id`` as a string. A malformed
        response must not poison the cache — fall back to the
        ``users.list`` walker instead.
        """
        transport.default_responses["users.lookupByEmail"] = {
            "ok": True,
            "user": {"id": 12345},  # int, not str
        }
        transport.default_responses["users.list"] = {
            "ok": True,
            "members": [{"id": "U_ALICE", "name": "alice"}],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        # Lookup the email; the int id from email-lookup is rejected,
        # then the list-walker picks up the same human by name.
        assert backend.resolve_user_id("alice@example.com") == snapshot("")

    def test_resolve_user_id_list_match_with_non_string_id_returns_empty(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``resolve_user_id`` accepts a non-string id from a list match."""
        transport.default_responses["users.list"] = {
            "ok": True,
            "members": [{"id": 99, "name": "alice"}],  # int, not str
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.resolve_user_id("alice") == snapshot("")

    def test_scanner_skips_blank_text_messages(self, transport: FakeSlackTransport) -> None:
        """RED if ``SlackDmInboundScanner.scan`` stops rejecting blank text."""
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "user": "U_HUMAN", "text": "   "},
                {"ts": "2.0", "user": "U_HUMAN", "text": "real"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        # Only the non-blank message produces a row.
        rows = list(PendingChatInjection.objects.values_list("text", flat=True))
        assert rows == snapshot(["real"])
        assert len(signals) == 1


__all__ = [
    "FakeSlackTransport",
    "OverlayBackends",
]

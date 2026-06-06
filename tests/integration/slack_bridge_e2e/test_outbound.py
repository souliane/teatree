"""Outbound bridge: notify_user → backend → chat.postMessage."""

from typing import Any
from unittest.mock import patch

import pytest
from inline_snapshot import snapshot

from teatree.backends.slack import http as slack_http
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core import backend_factory
from teatree.core import notify as core_notify
from teatree.core.backend_factory import iter_overlay_backends, messaging_from_overlay
from teatree.core.models import BotPing
from teatree.notify import NotifyKind, notify_user
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, _FakeConfig

pytestmark = [pytest.mark.django_db, pytest.mark.integration]


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

        # Justified scaffolding (#1066 nit 2): patching the config-resolution
        # seam (``_messaging_from_toml`` / ``teatree.config.load_config`` /
        # ``get_overlay``) is how multiple synthetic per-overlay configs are
        # injected — they are not expressible via a single real
        # ``~/.teatree.toml``. ``httpx`` stays the only real external. See
        # the conftest module docstring; do not rewrite this to real-TOML.
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
        monkeypatch.setattr(slack_http.httpx, "post", recording.post)
        monkeypatch.setattr(slack_http.httpx, "get", recording.get)

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

        # Justified scaffolding (#1066 nit 2): synthetic TOML-only overlay
        # config + ``read_pass`` stub. See the conftest module docstring.
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
        # Patch on the CONSUMER namespace: notify.py does
        # `from teatree.core.backend_factory import messaging_from_overlay`,
        # binding the name into teatree.core.notify. Patching the definition
        # site (backend_factory.messaging_from_overlay) would NOT intercept
        # notify_user's already-bound reference. If notify.py ever switches to
        # `import backend_factory` + `backend_factory.messaging_from_overlay(...)`,
        # move this patch target to backend_factory accordingly.
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

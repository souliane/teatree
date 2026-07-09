"""Per-overlay routing surface + xoxp/xoxb token-selection policy."""

from dataclasses import dataclass
from unittest.mock import patch

import pytest
from inline_snapshot import snapshot

from teatree.backends.slack.bot import SlackBotBackend
from teatree.core import backend_factory
from teatree.core.backend_factory import iter_overlay_backends, messaging_from_overlay
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, _FakeConfig

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [pytest.mark.django_db, pytest.mark.integration]


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

        # Justified scaffolding (#1066 nit 2): the per-overlay routing tests
        # deliberately patch ``teatree.config.load_config`` /
        # ``backend_factory.get_overlay`` (and stub ``read_pass``) to inject
        # multiple synthetic overlay configs that a single real
        # config store cannot express. ``httpx`` (network) and the
        # password store are the only true externals and stay real. See the
        # conftest module docstring; do not "fix" this back to real-TOML.
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

        from teatree.backends.backend_provider import SlackBackendProvider  # noqa: PLC0415

        # A real provider keeps ``build_slack_messaging`` live (so the TOML
        # overlay builds a real ``SlackBotBackend``); only the entry-point
        # credential resolution is neutralised — the synthetic ``py-overlay``
        # has no live backends.
        provider = SlackBackendProvider()

        # Justified scaffolding (#1066 nit 2): synthetic entry-point + TOML
        # overlays injected via patched ``get_all_overlays`` /
        # ``load_config`` + a ``read_pass`` stub. See the conftest module
        # docstring; the network (``httpx``) stays real.
        with (
            patch.object(backend_factory, "get_all_overlays", return_value={"py-overlay": py_overlay}),
            patch.object(backend_factory, "get_backend_provider", return_value=provider),
            patch.object(provider, "get_code_hosts", return_value=[]),
            patch.object(provider, "get_messaging", return_value=None),
            patch("teatree.config.load_config", return_value=_FakeConfig(raw={"overlays": cfg_overlays})),
            patch("teatree.utils.secrets.read_pass", side_effect=lambda k: pass_lookup.get(k, "")),
        ):
            result = iter_overlay_backends()

        names = sorted(b.name for b in result)
        assert names == snapshot(["py-overlay", "toml-overlay"])


class TestXoxpVsXoxbRouting:
    """Slack-Connect channels route through ``xoxp-…``; DMs stay on ``xoxb-…`` (#1072).

    The token-selection policy (``SlackBotBackend._channel_token``)
    resolves Connect membership deterministically via
    ``conversations.info`` and is the single point every outbound
    surface consults. The pre-#1072 ``_reaction_token`` routed *all*
    reactions through ``xoxp`` whenever it was configured; the
    systematic policy routes only externally-shared channels.
    """

    def test_connect_channel_reactions_route_through_xoxp_token(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``react()`` stops consulting ``_channel_token()``.

        Guard: removing the ``token=self._channel_token(channel)`` kwarg
        in ``SlackBotBackend.react`` makes the reaction post under the
        bot token, which Slack-Connect rejects with
        ``mcp_externally_shared_channel_restricted``.
        """
        transport.default_responses["conversations.info"] = {
            "ok": True,
            "channel": {"is_ext_shared": True},
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        backend.react(channel="C-CONNECT", ts="1.0", emoji="eyes")

        react_calls = transport.calls_to("reactions.add")
        assert len(react_calls) == 1
        assert react_calls[0].token == snapshot("xoxp-user")
        # Connect membership was probed with the bot token.
        info_calls = transport.calls_to("conversations.info")
        assert len(info_calls) == 1
        assert info_calls[0].token == snapshot("xoxb-bot")

    def test_internal_channel_reactions_stay_on_xoxb(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if reactions on internal channels stop using the bot token.

        The pre-#1072 bug: a configured ``xoxp`` hijacked *every*
        reaction, even on internal channels where the bot token already
        has ``reactions:write``. The default transport reports the
        channel as not externally-shared.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        backend.react(channel="C-INTERNAL", ts="1.0", emoji="eyes")

        react_calls = transport.calls_to("reactions.add")
        assert len(react_calls) == 1
        assert react_calls[0].token == snapshot("xoxb-bot")

    def test_connect_channel_post_routes_through_xoxp_token(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``post_message`` stops consulting ``_channel_token()``.

        The capability #1072 adds: posting to a Slack-Connect channel
        the bot cannot reach goes out under the user's ``xoxp`` identity.
        """
        transport.default_responses["conversations.info"] = {
            "ok": True,
            "channel": {"is_ext_shared": True},
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        backend.post_message(channel="C-CONNECT", text="review please")

        post_calls = transport.calls_to("chat.postMessage")
        assert len(post_calls) == 1
        assert post_calls[0].token == snapshot("xoxp-user")

    def test_dm_post_stays_on_xoxb(self, transport: FakeSlackTransport) -> None:
        """RED if a DM post is rerouted off the bot token.

        Guard: routing a ``D…`` channel through ``xoxp`` would
        impersonate the user against the bot's own DM history. DMs never
        even trigger a Connect-membership probe.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        backend.post_message(channel="D-USER", text="hi")

        post_calls = transport.calls_to("chat.postMessage")
        assert len(post_calls) == 1
        assert post_calls[0].token == snapshot("xoxb-bot")
        assert transport.calls_to("conversations.info") == []

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


class TestAmbiguousConnectChannelWriteFailsTowardUser:
    """Unconfirmable Connect membership: WRITE/REACT -> xoxp; DM -> xoxb (#1110).

    When ``conversations.info`` cannot confirm membership (``ok:false``
    — bad token, missing scope, not-found, rate-limit), the pre-#1110
    policy silently routed the write to the bot token, which a
    Slack-Connect channel rejects with
    ``mcp_externally_shared_channel_restricted`` — the partner write or
    the reactive-answer ack vanished. #1110: a WRITE / reaction in an
    unconfirmable channel fails *toward* the user ``xoxp`` token. DMs
    still short-circuit to the bot token before any probe (the 131-row
    DM drain regression pin).
    """

    def test_reactive_answer_cycle_on_ambiguous_connect_uses_xoxp(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """A react+post cycle on an unconfirmable Connect channel -> xoxp.

        RED on main: ``conversations.info`` ``ok:false`` -> the policy
        treats the channel as internal and both ``reactions.add`` and
        ``chat.postMessage`` go out under the bot token (xoxb), which
        Slack-Connect rejects.
        """
        transport.default_responses["conversations.info"] = {
            "ok": False,
            "error": "channel_not_found",
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        backend.react(channel="C-CONNECT", ts="1.0", emoji="eyes")
        backend.post_message(channel="C-CONNECT", text="answer")

        react_calls = transport.calls_to("reactions.add")
        assert len(react_calls) == 1
        assert react_calls[0].token == snapshot("xoxp-user")
        post_calls = transport.calls_to("chat.postMessage")
        assert len(post_calls) == 1
        assert post_calls[0].token == snapshot("xoxp-user")

    def test_dm_react_on_ambiguous_info_stays_on_bot(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """A DM react stays on xoxb and never probes (131-row-drain pin).

        Even with a globally-broken ``conversations.info``, a ``D…``
        channel short-circuits to the bot token *before* any probe — the
        #1110 ambiguous-WRITE branch must never reroute DMs.
        """
        transport.default_responses["conversations.info"] = {
            "ok": False,
            "error": "ratelimited",
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        backend.react(channel="D-USER", ts="1.0", emoji="eyes")

        react_calls = transport.calls_to("reactions.add")
        assert len(react_calls) == 1
        assert react_calls[0].token == snapshot("xoxb-bot")
        assert transport.calls_to("conversations.info") == []

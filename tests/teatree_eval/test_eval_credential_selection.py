"""The eval lane's credential is derived from ``agent_harness_provider``.

``resolve_eval_credential`` is the single seam every eval chokepoint reads, so the
provider→credential mapping is the whole selection surface: no provider pinned (and
an explicit subscription pin) rides the plan's OAuth token; the two
Anthropic-metered providers ride ``ANTHROPIC_API_KEY``; a BYOK-router pin has no
eval credential of its own and falls back to OAuth with a warning rather than
failing a validly-configured deployment. ``make_runner(API_BACKEND, ...)`` — the
metered-runner chokepoint — inherits that choice, and the isolation env strips
exactly the selected credential's conflicting vars.
"""

import logging
import os
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import AgentHarnessProvider
from teatree.core.models import ConfigSetting
from teatree.credential_config import resolve_eval_credential
from teatree.eval.api_runner import ApiInProcessRunner
from teatree.eval.backends import API_BACKEND, make_runner
from teatree.eval.isolation import isolated_claude_env
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential

_OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
_API_KEY_ENV = "ANTHROPIC_API_KEY"

_PROVIDER_TO_CREDENTIAL = [
    (None, AnthropicSubscriptionCredential),
    (AgentHarnessProvider.SUBSCRIPTION_OAUTH, AnthropicSubscriptionCredential),
    (AgentHarnessProvider.API_KEY, AnthropicApiKeyCredential),
    (AgentHarnessProvider.ANTHROPIC_API, AnthropicApiKeyCredential),
    (AgentHarnessProvider.ORCA_ROUTER_BYOK, AnthropicSubscriptionCredential),
]


class TestProviderToCredentialMapping(TestCase):
    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS_PROVIDER", raising=False)
        monkeypatch.delenv("T3_EVAL_IN_CONTAINER", raising=False)

    def test_explicit_kind_maps_to_its_credential_class(self) -> None:
        for provider, expected in _PROVIDER_TO_CREDENTIAL:
            with self.subTest(provider=str(provider)):
                assert isinstance(resolve_eval_credential(kind=provider), expected)

    def test_configured_provider_maps_to_its_credential_class(self) -> None:
        for provider, expected in _PROVIDER_TO_CREDENTIAL:
            if provider is None:
                continue
            with self.subTest(provider=str(provider)):
                ConfigSetting.objects.set_value("agent_harness_provider", provider.value)
                assert isinstance(resolve_eval_credential(), expected)

    def test_unconfigured_provider_defaults_to_subscription_oauth(self) -> None:
        assert isinstance(resolve_eval_credential(), AnthropicSubscriptionCredential)

    def test_byok_router_falls_back_to_oauth_with_a_warning(self) -> None:
        with self.assertLogs("teatree.credential_config", level=logging.WARNING) as logs:
            credential = resolve_eval_credential(kind=AgentHarnessProvider.ORCA_ROUTER_BYOK)
        assert isinstance(credential, AnthropicSubscriptionCredential)
        assert "orca_router_byok" in "\n".join(logs.output)

    def test_only_the_byok_row_warns(self) -> None:
        for provider, _expected in _PROVIDER_TO_CREDENTIAL:
            if provider is AgentHarnessProvider.ORCA_ROUTER_BYOK:
                continue
            with self.subTest(provider=str(provider)), patch("teatree.credential_config.logger") as spy:
                resolve_eval_credential(kind=provider)
                spy.warning.assert_not_called()


class TestMakeRunnerSelectsEvalCredential(TestCase):
    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_AGENT_HARNESS_PROVIDER", raising=False)

    def test_default_lane_exports_the_oauth_token_and_strips_the_api_key(self) -> None:
        with patch.dict(os.environ, {_OAUTH_ENV: "oauth-sub", _API_KEY_ENV: "sk-metered"}, clear=False):
            runner = make_runner(API_BACKEND)
        assert isinstance(runner, ApiInProcessRunner)
        # The subscription lane strips the metered API key so the SDK can't bill it.
        assert runner._conflicting_vars == (_API_KEY_ENV,)

    def test_default_lane_fails_loud_when_no_oauth_token_is_resolvable(self) -> None:
        from teatree.llm.credentials import CredentialError  # noqa: PLC0415

        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.llm.credentials.read_pass", return_value=""),
        ):
            os.environ.pop(_OAUTH_ENV, None)
            with pytest.raises(CredentialError):
                make_runner(API_BACKEND)

    def test_api_key_provider_exports_the_api_key_and_strips_the_oauth_token(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "api_key")
        with patch.dict(os.environ, {_OAUTH_ENV: "oauth-sub", _API_KEY_ENV: "sk-metered"}, clear=False):
            runner = make_runner(API_BACKEND)
        assert isinstance(runner, ApiInProcessRunner)
        assert runner._conflicting_vars == (_OAUTH_ENV,)


class TestIsolationStripsTheSelectedConflict:
    def test_oauth_lane_keeps_the_oauth_token_and_strips_the_api_key(self) -> None:
        sentinel = {_OAUTH_ENV: "oauth-sub", _API_KEY_ENV: "sk-metered"}
        with patch.dict(os.environ, sentinel, clear=False), isolated_claude_env((_API_KEY_ENV,)) as (env, _cwd):
            assert env[_OAUTH_ENV] == "oauth-sub"
            assert _API_KEY_ENV not in env

    def test_metered_lane_keeps_the_api_key_and_strips_the_oauth_token(self) -> None:
        sentinel = {_OAUTH_ENV: "oauth-sub", _API_KEY_ENV: "sk-metered"}
        with patch.dict(os.environ, sentinel, clear=False), isolated_claude_env((_OAUTH_ENV,)) as (env, _cwd):
            assert env[_API_KEY_ENV] == "sk-metered"
            assert _OAUTH_ENV not in env

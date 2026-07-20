"""A base-URL redirect can never reach a subscription-authenticated ``claude`` child.

Both the ``claude`` CLI and the Anthropic SDK read ``ANTHROPIC_BASE_URL`` natively, and
the SDK transport spawns its child off the inherited environment — so an ambient value
redirects every request that child makes. Subscription (plan) auth is only valid against
Anthropic's own endpoint, so carrying it to another host is never what an operator meant.

The refusal has two seams, because a dispatch has two shapes. With a Layer-2 provider
PINNED, the credential's own ``forbidden_vars`` rule refuses at ``child_env``. With NO
pin (the shipped default) the CLI authenticates from its own login state, which this
process cannot observe — so the ambient guard refuses unless the environment carries the
one unambiguously-sanctioned shape: a metered key with no subscription token beside it.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from django.test import TestCase

import teatree.agents.harness as harness_mod
import teatree.agents.headless as headless_mod
from teatree.agents.headless import _provider_child_env, run_headless
from teatree.config import AgentHarnessProvider
from teatree.core.models import Session, Task, Ticket
from teatree.llm.credentials import CredentialError
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream

# Spelled as literals, NOT imported from the module under test: these names are the
# contract the ``claude`` CLI and the Anthropic SDK read, so the test must fail when
# the BEHAVIOUR is missing rather than merely when a teatree constant is.
ANTHROPIC_BASE_URL_ENV = "ANTHROPIC_BASE_URL"
_API_KEY = "ANTHROPIC_API_KEY"
_OAUTH = "CLAUDE_CODE_OAUTH_TOKEN"
_GATEWAY = "https://gateway.example.invalid/v1"


@contextmanager
def _ambient(**values: str) -> Iterator[None]:
    """Patch ``os.environ`` so exactly the named auth vars are set and no others.

    ``patch.dict`` restores the whole mapping on exit, including the keys popped
    here, so a developer's real ambient credentials are neither read nor disturbed.
    """
    with patch.dict(os.environ, values, clear=False):
        for var in (ANTHROPIC_BASE_URL_ENV, _API_KEY, _OAUTH):
            if var not in values:
                os.environ.pop(var, None)
        yield


class TestAmbientDispatchRefusesBaseUrlRedirect(TestCase):
    """No Layer-2 pin: the CLI's own login state is unobservable, so the guard is conservative."""

    def test_refuses_when_a_subscription_token_rides_alongside_the_redirect(self) -> None:
        with _ambient(**{ANTHROPIC_BASE_URL_ENV: _GATEWAY, _OAUTH: "sk-ant-oat01-x"}):
            with self.assertRaises(CredentialError) as caught:
                _provider_child_env(None)
        assert ANTHROPIC_BASE_URL_ENV in str(caught.exception)
        assert "api_key" in str(caught.exception), "the refusal must name the sanctioned remedy"

    def test_refuses_when_no_credential_is_ambient_because_the_cli_falls_back_to_its_login(self) -> None:
        # Nothing in the env names a credential, so the CLI uses its stored login —
        # on a plan deployment that is the subscription. Refuse rather than guess.
        with _ambient(**{ANTHROPIC_BASE_URL_ENV: _GATEWAY}):
            with self.assertRaises(CredentialError):
                _provider_child_env(None)

    def test_refuses_when_both_credentials_are_ambient(self) -> None:
        with _ambient(**{ANTHROPIC_BASE_URL_ENV: _GATEWAY, _API_KEY: "sk-ant-key", _OAUTH: "sk-ant-oat01-x"}):
            with self.assertRaises(CredentialError):
                _provider_child_env(None)

    def test_allows_a_metered_key_pointed_at_a_gateway(self) -> None:
        # The sanctioned shape: an operator's OWN API key routed through a gateway,
        # Bedrock/Vertex, or an Anthropic-compatible third-party provider.
        with _ambient(**{ANTHROPIC_BASE_URL_ENV: _GATEWAY, _API_KEY: "sk-ant-key"}):
            assert _provider_child_env(None) is None

    def test_allows_an_ambient_dispatch_with_no_redirect_configured(self) -> None:
        with _ambient(**{_OAUTH: "sk-ant-oat01-x"}):
            assert _provider_child_env(None) is None

    def test_an_empty_redirect_value_expresses_nothing_and_is_ignored(self) -> None:
        with _ambient(**{ANTHROPIC_BASE_URL_ENV: "   ", _OAUTH: "sk-ant-oat01-x"}):
            assert _provider_child_env(None) is None


class TestPinnedProviderRefusesBaseUrlRedirect(TestCase):
    """With a pin, the credential's own ``forbidden_vars`` rule is what refuses."""

    def test_subscription_pin_refuses_the_redirect(self) -> None:
        with _ambient(**{ANTHROPIC_BASE_URL_ENV: _GATEWAY, _OAUTH: "sk-ant-oat01-x"}):
            with self.assertRaises(CredentialError) as caught:
                _provider_child_env(AgentHarnessProvider.SUBSCRIPTION_OAUTH)
        assert ANTHROPIC_BASE_URL_ENV in str(caught.exception)

    def test_api_key_pin_carries_the_redirect_through_to_the_child(self) -> None:
        with _ambient(**{ANTHROPIC_BASE_URL_ENV: _GATEWAY, _API_KEY: "sk-ant-key"}):
            env = _provider_child_env(AgentHarnessProvider.API_KEY)

        assert env is not None
        assert env[ANTHROPIC_BASE_URL_ENV] == _GATEWAY
        assert env[_API_KEY] == "sk-ant-key"
        assert _OAUTH not in env


class TestDispatchRecordsTheRefusalRatherThanRunning(TestCase):
    """End to end: the guard fails the dispatch loud instead of silently redirecting."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_run_headless_records_a_failed_attempt_naming_the_variable(self) -> None:
        spawned: list[object] = []

        def _make_client(*, options: object = None, **_: object) -> FakeHarnessSession:
            spawned.append(options)
            return FakeHarnessSession(success_stream({"summary": "ok"}))

        snapshot = headless_mod.TaskUsage(turns=0, cost_usd=0.0)
        with (
            _ambient(**{ANTHROPIC_BASE_URL_ENV: _GATEWAY, _OAUTH: "sk-ant-oat01-x"}),
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(harness_mod, "ClaudeSDKClient", _make_client),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
        ):
            session = Session.objects.create(ticket=self.ticket, agent_id="base-url-guard")
            task = Task.objects.create(ticket=self.ticket, session=session)
            run_headless(task, phase="coding", overlay_skill_metadata={})

        assert spawned == [], "no claude child may be spawned once the redirect is refused"
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        attempt = task.attempts.order_by("-pk").first()
        assert attempt is not None
        assert ANTHROPIC_BASE_URL_ENV in attempt.error

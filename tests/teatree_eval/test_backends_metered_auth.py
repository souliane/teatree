"""``make_runner(API_BACKEND, …)`` authenticates the eval lane via the SELECTED credential.

The fresh-run ``api`` runner is constructed ONLY through
:func:`teatree.eval.backends.make_runner` (the chokepoint enforced by
``tests/quality/test_metered_runner_chokepoint.py``). That factory resolves the eval
credential through :func:`teatree.credential_config.resolve_eval_credential` — the
DEFAULT is the subscription OAuth token (reversing #2707), and the metered
``ANTHROPIC_API_KEY`` is selectable via the ``eval_credential`` knob — before the
runner exists. With no credential available the construction fails loud with
:class:`~teatree.llm.credentials.CredentialError`: the eval lane refuses to run
authenticated as nothing rather than silently falling back.
"""

import os
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.eval.api_runner import ApiInProcessRunner
from teatree.eval.backends import API_BACKEND, TRANSCRIPT_BACKEND, make_runner
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential, CredentialError

_API_KEY_ENV = AnthropicApiKeyCredential().spec.env_var
_OAUTH_ENV = AnthropicSubscriptionCredential().spec.env_var


class TestMakeRunnerEvalAuth(TestCase):
    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_EVAL_CREDENTIAL", raising=False)

    def test_default_api_backend_resolves_the_oauth_token_before_building_the_runner(self) -> None:
        with patch.object(AnthropicSubscriptionCredential, "export", return_value="oauth-sub") as export:
            runner = make_runner(API_BACKEND)
        export.assert_called_once_with()
        assert isinstance(runner, ApiInProcessRunner)
        assert runner._conflicting_vars == (_API_KEY_ENV,)

    def test_metered_knob_resolves_the_api_key_before_building_the_runner(self) -> None:
        ConfigSetting.objects.set_value("eval_credential", "metered_api_key")
        with patch.object(AnthropicApiKeyCredential, "export", return_value="sk-key") as export:
            runner = make_runner(API_BACKEND)
        export.assert_called_once_with()
        assert isinstance(runner, ApiInProcessRunner)
        assert runner._conflicting_vars == (_OAUTH_ENV,)

    def test_api_backend_fails_loud_when_no_credential_is_available(self) -> None:
        # No token in env, pass empty → the factory must raise the typed credential
        # error rather than constructing a runner that authenticates as nothing.
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.llm.credentials.read_pass", return_value=""),
        ):
            os.environ.pop(_OAUTH_ENV, None)
            os.environ.pop(_API_KEY_ENV, None)
            with pytest.raises(CredentialError):
                make_runner(API_BACKEND)

    def test_transcript_backend_never_resolves_a_credential(self) -> None:
        # The transcript lane runs no model, so it must not resolve any credential.
        with (
            patch.object(AnthropicSubscriptionCredential, "export") as oauth_export,
            patch.object(AnthropicApiKeyCredential, "export") as key_export,
        ):
            make_runner(TRANSCRIPT_BACKEND)
        oauth_export.assert_not_called()
        key_export.assert_not_called()

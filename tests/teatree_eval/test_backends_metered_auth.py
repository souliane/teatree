"""``make_runner(SDK_BACKEND, …)`` authenticates the metered lane via the API key.

The metered ``sdk`` runner is constructed ONLY through
:func:`teatree.eval.backends.make_runner` (the chokepoint enforced by
``tests/quality/test_metered_runner_chokepoint.py``). That factory resolves the
metered ``ANTHROPIC_API_KEY`` through the canonical credential layer
(:class:`teatree.llm.credentials.AnthropicApiKeyCredential`) — never the
subscription OAuth token — before the runner exists. With no key available the
construction fails loud with :class:`~teatree.llm.credentials.CredentialError`:
the metered eval refuses to run on the subscription rather than silently
throttling against it.
"""

import os
from unittest.mock import patch

import pytest

from teatree.eval.backends import SDK_BACKEND, TRANSCRIPT_BACKEND, make_runner
from teatree.eval.sdk_runner import SdkInProcessRunner
from teatree.llm.credentials import AnthropicApiKeyCredential, CredentialError

_API_KEY_ENV = AnthropicApiKeyCredential().spec.env_var


class TestMakeRunnerMeteredAuth:
    def test_sdk_backend_resolves_the_api_key_before_building_the_runner(self) -> None:
        with patch.object(AnthropicApiKeyCredential, "export", return_value="sk-key") as export:
            runner = make_runner(SDK_BACKEND)
        export.assert_called_once_with()
        assert isinstance(runner, SdkInProcessRunner)

    def test_sdk_backend_fails_loud_when_no_api_key_is_available(self) -> None:
        # No env var, pass empty → the factory must raise the typed credential error
        # rather than constructing a runner that would throttle the subscription.
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.llm.credentials.read_pass", return_value=""),
        ):
            os.environ.pop(_API_KEY_ENV, None)
            with pytest.raises(CredentialError):
                make_runner(SDK_BACKEND)

    def test_transcript_backend_never_resolves_the_api_key(self) -> None:
        # The transcript lane runs no model, so it must not resolve any credential.
        with patch.object(AnthropicApiKeyCredential, "export") as export:
            make_runner(TRANSCRIPT_BACKEND)
        export.assert_not_called()

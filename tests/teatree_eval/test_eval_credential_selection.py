"""The eval lane rides the credential the ``eval_credential`` knob selects (#2707 reversal).

``make_runner(API_BACKEND, ...)`` — the single metered-runner chokepoint — resolves
its credential through ``resolve_eval_credential``, so the default reverses #2707: the
fresh-run lane authenticates with the subscription OAuth token and the isolated child
strips the metered API key. Flip the ``eval_credential`` knob to ``metered_api_key``
and it exports the API key and strips the OAuth token instead. The isolation env
strips exactly the selected credential's conflicting vars.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.eval.api_runner import ApiInProcessRunner
from teatree.eval.backends import API_BACKEND, make_runner
from teatree.eval.isolation import isolated_claude_env

_OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
_API_KEY_ENV = "ANTHROPIC_API_KEY"


class TestMakeRunnerSelectsEvalCredential(TestCase):
    @pytest.fixture(autouse=True)
    def _isolate_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.config.CONFIG_PATH", tmp_path / ".teatree.toml")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_EVAL_CREDENTIAL", raising=False)

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

    def test_metered_knob_exports_the_api_key_and_strips_the_oauth_token(self) -> None:
        ConfigSetting.objects.set_value("eval_credential", "metered_api_key")
        with patch.dict(os.environ, {_OAUTH_ENV: "oauth-sub", _API_KEY_ENV: "sk-metered"}, clear=False):
            runner = make_runner(API_BACKEND)
        assert isinstance(runner, ApiInProcessRunner)
        # The metered lane strips the subscription OAuth token (the pre-#2707 shape).
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

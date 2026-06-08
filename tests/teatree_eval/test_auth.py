"""The metered sdk eval lane resolves ``CLAUDE_CODE_OAUTH_TOKEN`` for itself.

The OAuth token authenticates ``claude -p``. CI provides it as an env var
(wired from the ``CLAUDE_CODE_OAUTH_TOKEN`` repo secret); locally it lives in
the ``pass`` store under ``anthropic/oauth-token``. :func:`ensure_oauth_token`
is the single resolver both lanes call before shelling ``claude``: an env var
already set wins (CI), otherwise it falls back to ``pass`` (local) and exports
the value so the host runner's ``isolated_claude_env`` copy and the docker
``-e CLAUDE_CODE_OAUTH_TOKEN`` pass-through both carry it.
"""

import os
from unittest.mock import patch

from teatree.eval.auth import OAUTH_TOKEN_ENV, OAUTH_TOKEN_PASS_KEY, ensure_oauth_token


class TestEnsureOAuthToken:
    def test_env_var_wins_and_pass_is_not_consulted(self) -> None:
        with (
            patch.dict(os.environ, {OAUTH_TOKEN_ENV: "ci-env-token"}, clear=False),
            patch("teatree.eval.auth.read_pass") as read_pass,
        ):
            token = ensure_oauth_token()
        assert token == "ci-env-token"
        read_pass.assert_not_called()

    def test_falls_back_to_pass_and_exports_when_env_absent(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.eval.auth.read_pass", return_value="pass-token") as read_pass,
        ):
            os.environ.pop(OAUTH_TOKEN_ENV, None)
            token = ensure_oauth_token()
            exported = os.environ.get(OAUTH_TOKEN_ENV)
        read_pass.assert_called_once_with(OAUTH_TOKEN_PASS_KEY)
        assert token == "pass-token"
        assert exported == "pass-token", "the pass-resolved token must be exported for docker -e / isolated_claude_env"

    def test_returns_none_and_exports_nothing_when_neither_present(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.eval.auth.read_pass", return_value=""),
        ):
            os.environ.pop(OAUTH_TOKEN_ENV, None)
            token = ensure_oauth_token()
            exported = os.environ.get(OAUTH_TOKEN_ENV)
        assert token is None
        assert exported is None, "a missing token must not export an empty env var (it would mask the real absence)"

    def test_empty_env_var_falls_back_to_pass(self) -> None:
        with (
            patch.dict(os.environ, {OAUTH_TOKEN_ENV: ""}, clear=False),
            patch("teatree.eval.auth.read_pass", return_value="pass-token"),
        ):
            token = ensure_oauth_token()
        assert token == "pass-token", "an empty env var is not a real token — fall back to pass"

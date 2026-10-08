"""A Codex dispatch carries the overlay's routed GitHub token and its pass key, and no other credential or store."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase

from teatree import forge_credentials
from teatree.agents._runner_env import XDIST_WORKERS_VAR, DispatchCredential
from teatree.agents.codex_app_server import CodexAppServerHarness
from teatree.agents.codex_shared_app_server import reset_shared_codex_app_servers
from teatree.agents.runner import _resolve_child_env_or_failure
from teatree.core.models import Session, Task
from teatree.forge_credentials import ROUTED_GH_KEY_ENV, ForgeCredentialRequest, ForgeTokenResolution, ForgeTokenState
from tests.factories import planned_ticket
from tests.teatree_agents.test_codex_shared_app_server import _SERVER, FakeCache

_DUMPING_SERVER = "import json, os, sys\nopen(sys.argv[1], 'w').write(json.dumps(dict(os.environ)))\n" + _SERVER
_AMBIENT_SECRETS = {
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
    "GITLAB_TOKEN": "gitlab-secret",
    "T3_ADMIN_PASSWORD": "admin-secret",
    "NOTION_TOKEN": "notion-secret",
    "T3_AGENT_MAILBOX_TOKEN": "mailbox-secret",
    "GH_TOKEN": "ambient-gh-secret",
    "PASSWORD_STORE_DIR": "/worker/.password-store",
    "GNUPGHOME": "/worker/.gnupg",
}
_PASS_KEY = "github/acme/pat"


def _provider(request: ForgeCredentialRequest) -> ForgeTokenResolution:
    if request.overlay_name == "acme":
        return ForgeTokenResolution(
            request.credential, "acme", ForgeTokenState.TOKEN, token="ghp-routed", pass_key=_PASS_KEY
        )
    return ForgeTokenResolution(request.credential, request.overlay_name, ForgeTokenState.UNSET)


class TestCodexForgeCredential(TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.object(forge_credentials, "_provider", _provider))
        self.addCleanup(reset_shared_codex_app_servers)

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _task(self, overlay: str) -> Task:
        ticket = planned_ticket(overlay=overlay)
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

    def _harness(self) -> CodexAppServerHarness:
        return CodexAppServerHarness(refusal=None, code_home=self._tmp_path / "home")

    def test_the_routed_token_and_a_pytest_worker_cap_become_the_dispatch_env(self) -> None:
        credential = _resolve_child_env_or_failure(self._task("acme"), self._harness(), None)

        assert isinstance(credential, DispatchCredential)
        assert credential.env is not None
        assert credential.env["GH_TOKEN"] == "ghp-routed"
        assert credential.env[ROUTED_GH_KEY_ENV] == _PASS_KEY
        assert int(credential.env[XDIST_WORKERS_VAR]) >= 1

    def test_an_unresolved_route_yields_no_token_but_keeps_the_cap(self) -> None:
        credential = _resolve_child_env_or_failure(self._task("other"), self._harness(), None)

        assert isinstance(credential, DispatchCredential)
        assert credential.env is not None
        assert "GH_TOKEN" not in credential.env
        assert XDIST_WORKERS_VAR in credential.env

    def test_the_token_reaches_the_codex_child_and_no_ambient_secret_does(self) -> None:
        script = self._tmp_path / "dumping_server.py"
        script.write_text(_DUMPING_SERVER)
        dump = self._tmp_path / "child-env.json"
        credential = _resolve_child_env_or_failure(self._task("acme"), self._harness(), None)
        assert isinstance(credential, DispatchCredential)
        options = ClaudeAgentOptions(cwd=str(self._tmp_path), permission_mode="bypassPermissions", env=credential.env)

        async def open_and_close() -> None:
            async with self._harness().open(options):
                pass

        with (
            patch.dict(os.environ, _AMBIENT_SECRETS),
            patch(
                "teatree.agents.codex_app_server.codex_command", return_value=(sys.executable, str(script), str(dump))
            ),
            patch("teatree.agents.codex_shared_app_server.CodexAuthCache", lambda _code_home: FakeCache()),
        ):
            asyncio.run(open_and_close())

        child = json.loads(dump.read_text())
        assert child["GH_TOKEN"] == "ghp-routed"
        assert child[ROUTED_GH_KEY_ENV] == _PASS_KEY
        assert XDIST_WORKERS_VAR in child
        assert not {key for key in _AMBIENT_SECRETS if key != "GH_TOKEN"} & child.keys()
        assert not {"ambient-gh-secret", "gitlab-secret", "anthropic-secret"} & set(child.values())

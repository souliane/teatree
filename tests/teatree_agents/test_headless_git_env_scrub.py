"""A dispatched agent inherits a hermetic environment — no ``GIT_*`` overrides.

When a headless dispatch fires from inside a git hook (pre-commit/pre-push), the
process env carries ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_WORK_TREE``. The
claude-agent-sdk transport spawns its child with ``{**os.environ, ...,
**options.env}`` — a dict merge that cannot DELETE a key ``options.env`` omits —
so those overrides would reach the agent and hijack its ``git`` calls onto the
outer repo. The dispatch seam strips them from ``os.environ`` for the spawn
window (so the SDK-inherited base is clean) and builds any credential-pinned
``options.env`` off the stripped base too.
"""

import os
from typing import cast
from unittest.mock import patch

from django.test import TestCase

import teatree.agents.harness as harness_mod
import teatree.agents.headless as headless_mod
from teatree.agents.headless import _provider_child_env, run_headless
from teatree.config import AgentHarnessProvider
from teatree.core.models import Session, Task, Ticket
from teatree.llm.builtin_tools import KNOWN_BUILTIN_TOOLS
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream


class TestGitEnvStrippedAtDispatch(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_child_inherits_no_git_overrides_and_they_are_restored(self) -> None:
        captured: dict[str, set[str]] = {}

        def _make_client(*, options: object = None, **_: object) -> FakeHarnessSession:
            # os.environ here is the transport's inherited_env base for the child.
            captured["env"] = {k for k in os.environ if k.startswith("GIT_")}
            opt_env = getattr(options, "env", None) or {}
            captured["options_env"] = {k for k in opt_env if k.startswith("GIT_")}
            return FakeHarnessSession(
                success_stream({"summary": "ok", "files_modified": [{"path": "src/x.py", "action": "modified"}]})
            )

        snapshot = headless_mod.TaskUsage(turns=0, cost_usd=0.0)
        with (
            patch.dict(os.environ, {"GIT_DIR": "/outer/.git", "GIT_INDEX_FILE": "/outer/.git/index"}, clear=False),
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(harness_mod, "ClaudeSDKClient", _make_client),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
        ):
            session = Session.objects.create(ticket=self.ticket, agent_id="a1")
            task = Task.objects.create(ticket=self.ticket, session=session)
            run_headless(task, phase="coding", overlay_skill_metadata={})

            assert captured["env"] == set(), f"child inherits GIT_* from os.environ: {captured['env']}"
            assert captured["options_env"] == set()
            # Restored for the rest of the (possibly hook) process once dispatch ends.
            assert os.environ["GIT_DIR"] == "/outer/.git"

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestCredentialChildEnvStripsGitOverrides(TestCase):
    def test_api_key_child_env_carries_the_token_but_no_git_overrides(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key-y", "GIT_DIR": "/outer/.git"}, clear=False):
            env = _provider_child_env(AgentHarnessProvider.API_KEY)

        assert env is not None
        assert env["ANTHROPIC_API_KEY"] == "key-y"
        assert not any(k.startswith("GIT_") for k in env), "credential child env must not carry GIT_* overrides"


class TestReaderPhaseEnvScrubbedAtDispatch(TestCase):
    """#116 (RED scenario B2 + C1): a ``directive_reading`` spawn carries no secret, no tool.

    The reader spawn actually goes THROUGH ``reader_env_hermetic`` (belt) with
    ``options.env`` filtered by ``reader_child_env`` (suspenders), and its tool sources
    are suppressed (C1). RED if the scrub is unwired, denylist-leaky, or the tool sources
    are loaded.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_reader_child_base_and_options_env_carry_no_secret_and_no_tool_source(self) -> None:
        captured: dict[str, object] = {}

        def _make_client(*, options: object = None, **_: object) -> FakeHarnessSession:
            leaked = {"DATABASE_URL", "SENTRY_DSN", "AWS_ACCESS_KEY_ID", "SLACK_BOT_TOKEN"}
            captured["os_secrets"] = {k for k in os.environ if k in leaked}
            captured["os_has_path"] = "PATH" in os.environ
            opt_env = getattr(options, "env", None) or {}
            captured["options_env"] = dict(opt_env)
            captured["disallowed"] = set(getattr(options, "disallowed_tools", []) or [])
            captured["setting_sources"] = getattr(options, "setting_sources", "UNSET")
            captured["strict_mcp"] = getattr(options, "strict_mcp_config", None)
            candidate = {"is_directive": True, "normalized_constraint": "cap 1 PR"}
            return FakeHarnessSession(success_stream({"summary": "ok", "directive_candidate": candidate}))

        snapshot = headless_mod.TaskUsage(turns=0, cost_usd=0.0)
        secrets = {
            "ANTHROPIC_API_KEY": "sk-ant",
            "DATABASE_URL": "postgres://u:p@h/db",
            "SENTRY_DSN": "https://k@s.io/1",
            "AWS_ACCESS_KEY_ID": "AKIA",
            "SLACK_BOT_TOKEN": "xoxb",
        }
        with (
            patch.dict(os.environ, secrets, clear=False),
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(harness_mod, "ClaudeSDKClient", _make_client),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
        ):
            session = Session.objects.create(ticket=self.ticket, agent_id="reader-1")
            task = Task.objects.create(ticket=self.ticket, session=session)
            run_headless(task, phase="directive_reading", overlay_skill_metadata={})

            # belt: os.environ (the SDK-inherited child base) is reduced to the allowlist
            assert captured["os_secrets"] == set(), captured["os_secrets"]
            assert captured["os_has_path"] is True
            # suspenders: options.env keeps the inference credential, drops every secret
            options_env = cast("dict[str, str]", captured["options_env"])
            assert options_env.get("ANTHROPIC_API_KEY") == "sk-ant"
            assert "DATABASE_URL" not in options_env
            assert "SLACK_BOT_TOKEN" not in options_env
            # C1: no tool from any source — exhaustive built-in denylist + settings/mcp suppressed
            assert set(KNOWN_BUILTIN_TOOLS) <= cast("set[str]", captured["disallowed"])
            assert captured["setting_sources"] == []
            assert captured["strict_mcp"] is True
            # restored for the rest of the (possibly hook) process
            assert os.environ["DATABASE_URL"] == "postgres://u:p@h/db"

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

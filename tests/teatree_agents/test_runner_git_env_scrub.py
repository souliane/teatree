"""A dispatched agent inherits a hermetic environment — no ``GIT_*`` overrides.

When a agent dispatch fires from inside a git hook (pre-commit/pre-push), the
process env carries ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_WORK_TREE``. The
claude-agent-sdk transport spawns its child with ``{**os.environ, ...,
**options.env}`` — a dict merge that cannot DELETE a key ``options.env`` omits —
so those overrides would reach the agent and hijack its ``git`` calls onto the
outer repo. The dispatch seam strips them from ``os.environ`` for the spawn
window (so the SDK-inherited base is clean) and builds any credential-pinned
``options.env`` off the stripped base too.
"""

import os
from unittest.mock import patch

from django.test import TestCase

import teatree.agents.harness as harness_mod
import teatree.agents.runner as runner_mod
from teatree.agents.runner import _provider_child_env, run_agent
from teatree.config import AgentHarnessProvider
from teatree.core.models import Session, Task
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream


class TestGitEnvStrippedAtDispatch(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = planned_ticket()

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

        snapshot = runner_mod.TaskUsage(turns=0, cost_usd=0.0)
        with (
            patch.dict(os.environ, {"GIT_DIR": "/outer/.git", "GIT_INDEX_FILE": "/outer/.git/index"}, clear=False),
            patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(harness_mod, "ClaudeSDKClient", _make_client),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
        ):
            session = Session.objects.create(ticket=self.ticket, agent_id="a1")
            task = Task.objects.create(ticket=self.ticket, session=session)
            run_agent(task, phase="coding", overlay_skill_metadata={})

            assert captured["env"] == set(), f"child inherits GIT_* from os.environ: {captured['env']}"
            assert captured["options_env"] == set()
            # Restored for the rest of the (possibly hook) process once dispatch ends.
            assert os.environ["GIT_DIR"] == "/outer/.git"

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestVirtualEnvStrippedAtDispatch(TestCase):
    """A dispatched agent never inherits the dispatcher's ``VIRTUAL_ENV``.

    ``uv pip`` installs into ``VIRTUAL_ENV`` ahead of the project venv, so an agent spawned
    from a ``uv run`` parent that editable-installs its own checkout writes a ``.pth`` into
    the parent's SHARED venv — which dangles, and breaks every import through it, the moment
    that checkout is reaped.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = planned_ticket()

    def test_child_inherits_no_virtual_env_and_it_is_restored(self) -> None:
        captured: dict[str, object] = {}

        def _make_client(*, options: object = None, **_: object) -> FakeHarnessSession:
            captured["os_env"] = os.environ.get("VIRTUAL_ENV")
            captured["options_env"] = (getattr(options, "env", None) or {}).get("VIRTUAL_ENV")
            return FakeHarnessSession(
                success_stream({"summary": "ok", "files_modified": [{"path": "src/x.py", "action": "modified"}]})
            )

        snapshot = runner_mod.TaskUsage(turns=0, cost_usd=0.0)
        with (
            patch.dict(os.environ, {"VIRTUAL_ENV": "/shared/.venv"}, clear=False),
            patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(harness_mod, "ClaudeSDKClient", _make_client),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
        ):
            session = Session.objects.create(ticket=self.ticket, agent_id="venv-1")
            task = Task.objects.create(ticket=self.ticket, session=session)
            run_agent(task, phase="coding", overlay_skill_metadata={})

            assert captured == {"os_env": None, "options_env": None}
            assert os.environ["VIRTUAL_ENV"] == "/shared/.venv"

    def test_credential_child_env_carries_no_virtual_env(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key-y", "VIRTUAL_ENV": "/shared/.venv"}, clear=False):
            env = _provider_child_env(AgentHarnessProvider.API_KEY).env

        assert env is not None
        assert "VIRTUAL_ENV" not in env


class TestCredentialChildEnvStripsGitOverrides(TestCase):
    def test_api_key_child_env_carries_the_token_but_no_git_overrides(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key-y", "GIT_DIR": "/outer/.git"}, clear=False):
            env = _provider_child_env(AgentHarnessProvider.API_KEY).env

        assert env is not None
        assert env["ANTHROPIC_API_KEY"] == "key-y"
        assert not any(k.startswith("GIT_") for k in env), "credential child env must not carry GIT_* overrides"

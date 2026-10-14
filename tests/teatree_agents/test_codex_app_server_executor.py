"""The Codex harness binds the read-only executor only for a write-barring phase in the worker, probing at open."""

import asyncio
import json
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase

from teatree.agents import codex_app_server_options, codex_read_only_server
from teatree.agents.codex_app_server import CodexAppServerSession, _thread_params, codex_app_server_spec
from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions
from teatree.agents.codex_read_only_verdict import CLONE_ROOT
from teatree.agents.codex_shared_app_server import SharedCodexAppServer
from teatree.agents.harness_dispatch import resolve_dispatch_harness
from teatree.agents.harness_registry import (
    HarnessBuildContext,
    HarnessFallbackError,
    HarnessFallbackKind,
    select_harness,
)
from teatree.agents.sdk_tool_map import sdk_disallowed_tools_for_phase
from teatree.core.models import Session, Task
from teatree.core.models.config_setting import ConfigSetting
from tests.factories import planned_ticket
from tests.teatree_agents._executor_fakes import FIXTURES, FakeExecutor, attested

_WRITE_PHASES = ("coding", "testing", "e2e", "debugging")
_PLANNING = ClaudeAgentOptions(permission_mode="bypassPermissions", disallowed_tools=["Write", "Edit", "NotebookEdit"])


def _in_the_worker() -> ExitStack[bool | None]:
    stack = ExitStack()
    stack.enter_context(patch.object(codex_app_server_options, "container_is_the_sandbox", lambda: True))
    stack.enter_context(patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=True))
    stack.enter_context(patch("teatree.agents.codex_app_server.shutil.which", return_value="/usr/bin/codex"))
    return stack


@pytest.fixture
def worker() -> Iterator[None]:
    with _in_the_worker():
        yield
    codex_read_only_server.reset_executor_app_servers()


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pass_store: None) -> FakeExecutor:
    executor = FakeExecutor(tmp_path)
    for key, value in executor.install().items():
        monkeypatch.setenv(key, value)
    return executor


def _app_server_execs(fake: FakeExecutor) -> list[list[str]]:
    return [call for call in fake.calls if call[:2] == ["exec", "-i"] and "setsid" in call]


@pytest.mark.usefixtures("worker")
@pytest.mark.parametrize("phase", _WRITE_PHASES)
def test_write_phases_never_bind_the_executor(phase: str) -> None:
    harness = codex_app_server_spec().factory(HarnessBuildContext(phase=phase))
    options = ClaudeAgentOptions(
        cwd="/work/main",
        add_dirs=["/work/peer"],
        permission_mode="bypassPermissions",
        system_prompt="brief",
        model="gpt-x",
        disallowed_tools=list(sdk_disallowed_tools_for_phase(phase)),
    )
    golden = json.loads((FIXTURES / "write_phase_thread_params_at_base.json").read_text(encoding="utf-8"))

    assert harness.read_only_phase is None
    assert harness.capabilities.read_only_venue is False
    assert _thread_params(CodexAppServerOptions.from_sdk_options(options)) == golden[f"{phase}-container"]


@pytest.mark.usefixtures("worker")
def test_planning_binds_the_executor_without_the_teatree_mcp_server() -> None:
    harness = codex_app_server_spec().factory(HarnessBuildContext(phase="planning"))

    assert harness.read_only_phase == "planning"
    assert (harness.capabilities.read_only_venue, harness.capabilities.mcp) == (True, False)


@pytest.mark.usefixtures("worker")
def test_a_host_planning_harness_keeps_codex_own_read_only_sandbox() -> None:
    with patch("teatree.agents.codex_app_server.container_is_the_sandbox", return_value=False):
        harness = codex_app_server_spec().factory(HarnessBuildContext(phase="planning"))

    assert harness.read_only_phase is None


@pytest.mark.usefixtures("worker")
def test_open_reprobes_before_spawning(fake: FakeExecutor) -> None:
    context = HarnessBuildContext(phase="planning")
    assert select_harness(["codex_app_server"], context).spec.name == "codex_app_server"
    harness = codex_app_server_spec().factory(context)
    fake.probe["writes"]["/"] = "OK"
    fake.install()

    async def enter() -> None:
        async with harness.open(_PLANNING):
            pytest.fail("the harness opened")

    with pytest.raises(HarnessFallbackError, match="/ accepted a write") as refused:
        asyncio.run(enter())

    assert (refused.value.kind, refused.value.side_effects_started) == (HarnessFallbackKind.ACCESS, False)
    assert _app_server_execs(fake) == []


def test_local_session_refuses_attested_options(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    options = CodexAppServerOptions.from_sdk_options(_PLANNING, executor=attested(fake, monkeypatch))
    session = CodexAppServerSession(options, resume=None, code_home=tmp_path, command=(sys.executable, "-c", "pass"))

    with pytest.raises(CodexAppServerError, match="executor-attested options on the local transport"):
        asyncio.run(session.start())

    assert session.process is None


def test_shared_worker_server_refuses_attested_options(
    fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    options = CodexAppServerOptions.from_sdk_options(_PLANNING, executor=attested(fake, monkeypatch))
    manager = SharedCodexAppServer(code_home=tmp_path / "worker-home")

    with pytest.raises(CodexAppServerError, match="executor-attested options on the local transport"):
        asyncio.run(manager.open_session(options, None))

    assert manager.stopped


@pytest.mark.usefixtures("pass_store")
class TestAPinnedCodexHarnessNeverRunsPlanningWithoutAnExecutor(TestCase):
    def _planning_task(self) -> Task:
        ConfigSetting.objects.set_value("agent_harness", "codex_app_server")
        ticket = planned_ticket()
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="planning")

    def _refusal(self, env: dict[str, str]) -> HarnessFallbackError:
        task = self._planning_task()
        with _in_the_worker(), patch.dict("os.environ", env):
            dispatch = resolve_dispatch_harness(task, phase="planning")

            async def enter() -> None:
                async with dispatch.harness.open(_PLANNING):
                    self.fail("the harness opened")

            with pytest.raises(HarnessFallbackError) as refused:
                asyncio.run(enter())
        return refused.value

    def test_without_an_executor_login_the_factory_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeExecutor(Path(directory), with_login=False)
            refusal = self._refusal(fake.install())

        assert "it has no Codex login" in str(refusal)
        assert refusal.side_effects_started is False

    def test_with_a_writable_executor_open_refuses_before_anything_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeExecutor(Path(directory))
            fake.probe["writes"][CLONE_ROOT] = "OK"
            refusal = self._refusal(fake.install())

            assert _app_server_execs(fake) == []
        assert f"{CLONE_ROOT} accepted a write" in str(refusal)
        assert refusal.side_effects_started is False

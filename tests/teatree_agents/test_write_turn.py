"""The heal fixer's write turn: one bounded, uncompacted, write-capable turn on the checkout, through a harness."""

import asyncio
import contextlib
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase

from teatree.agents.compaction_guard import COMPACTION_BLOCKED_REASON
from teatree.agents.harness import ClaudeSdkHarness
from teatree.agents.write_turn import run_bounded_write_turn
from teatree.core.models import ConfigSetting
from teatree.utils.env import patched_environ
from tests.teatree_agents._sdk_fake import (
    FakeHarness,
    FakeHarnessSession,
    assert_uncompacted,
    assistant_text,
    fake_sdk,
    pre_compact_input,
)


class _ScriptedSession(FakeHarnessSession):
    def __init__(self, options: ClaudeAgentOptions, *, compacts: bool) -> None:
        super().__init__([assistant_text("edited the product")])
        self._options = options
        self._compacts = compacts
        self.prompt = ""
        self.drained = False

    async def query(self, prompt: str) -> None:
        self.prompt = prompt

    async def receive_response(self) -> AsyncIterator[object]:
        if self._compacts:
            [matcher] = (self._options.hooks or {})["PreCompact"]
            await matcher.hooks[0](pre_compact_input("auto"), None, {"signal": None})
            await asyncio.sleep(0)
        async for message in super().receive_response():
            yield message
        self.drained = not self.interrupted


class _ScriptedHarness(FakeHarness):
    def __init__(self, *, compacts: bool = False) -> None:
        super().__init__([])
        self._compacts = compacts
        self.sessions: list[_ScriptedSession] = []

    @contextlib.asynccontextmanager
    async def open(self, options: ClaudeAgentOptions) -> AsyncIterator[_ScriptedSession]:
        self.opened_options = options
        session = _ScriptedSession(options, compacts=self._compacts)
        self.sessions.append(session)
        yield session


class TestTheWriteTurn:
    def test_it_prompts_a_write_capable_session_on_the_checkout_and_reads_it_to_the_end(self, tmp_path: Path) -> None:
        harness = _ScriptedHarness()
        run_bounded_write_turn("fix the red", tmp_path, timeout_seconds=5, harness=harness)

        [session] = harness.sessions
        assert session.prompt == "fix the red"
        assert harness.opened_options.cwd == str(tmp_path)
        assert harness.opened_options.permission_mode == "bypassPermissions"
        assert session.drained

    def test_it_runs_with_compaction_off(self, tmp_path: Path) -> None:
        harness = _ScriptedHarness()

        run_bounded_write_turn("fix", tmp_path, timeout_seconds=5, harness=harness)

        assert_uncompacted(harness.opened_options)

    def test_an_automatic_compaction_interrupts_the_session_and_fails_the_turn(self, tmp_path: Path) -> None:
        harness = _ScriptedHarness(compacts=True)

        with pytest.raises(RuntimeError) as raised:
            run_bounded_write_turn("fix", tmp_path, timeout_seconds=5, harness=harness)

        [session] = harness.sessions
        assert str(raised.value) == COMPACTION_BLOCKED_REASON
        assert session.interrupted

    def test_a_turn_past_its_timeout_is_cut_off(self, tmp_path: Path) -> None:
        with pytest.raises(TimeoutError):
            run_bounded_write_turn(
                "fix", tmp_path, timeout_seconds=0.05, harness=FakeHarness([assistant_text("slow")], delay=5)
            )


class TestAWriteTurnOnAPinnedCredential(TestCase):
    def test_the_cli_child_carries_the_credential_and_the_compaction_switch(self) -> None:
        ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        checkout = Path(self.enterContext(tempfile.TemporaryDirectory()))

        with (
            patched_environ({"CLAUDE_CODE_OAUTH_TOKEN": "oauth-x"}, remove=("DISABLE_COMPACT",)),
            fake_sdk([]) as client,
        ):
            run_bounded_write_turn("fix", checkout, timeout_seconds=5, harness=ClaudeSdkHarness())

        assert_uncompacted(client.last_options)
        assert client.last_options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-x"

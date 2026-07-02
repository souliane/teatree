"""The ``Harness`` seam — backend resolution + the provider-agnostic driver (#2565).

``resolve_harness`` reads the DB-home ``agent_harness`` setting and returns the
transport backend: the default resolves to :class:`ClaudeSdkHarness` (byte-identical
to the pre-seam transport), the reserved ``pydantic_ai`` value raises a clear
``NotImplementedError`` until a later PR builds it, and the ``T3_AGENT_HARNESS``
env / ``ConfigSetting`` store are the switch. ``_drive_with_heartbeat`` talks only
to the narrow ``HarnessSession`` surface, so an arbitrary backend drives a run.
"""

import asyncio
import os
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.agents.headless as headless_mod
from teatree.agents.harness import ClaudeSdkHarness, resolve_harness
from teatree.agents.headless import LoopWatchdog, TaskUsage, _build_options, _drive_with_heartbeat, run_headless
from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket
from tests.teatree_agents._sdk_fake import FakeHarness, assistant_text, result_message


class TestResolveHarness(TestCase):
    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_AGENT_HARNESS", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_default_resolves_to_claude_sdk_backend(self) -> None:
        assert get_effective_settings().agent_harness.value == "claude_sdk"
        assert isinstance(resolve_harness(), ClaudeSdkHarness)

    def test_stored_claude_sdk_resolves_to_claude_sdk_backend(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        assert isinstance(resolve_harness(), ClaudeSdkHarness)

    def test_stored_pydantic_ai_raises_not_implemented(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        with pytest.raises(NotImplementedError, match="pydantic_ai"):
            resolve_harness()

    def test_env_switch_to_pydantic_ai_raises_not_implemented(self) -> None:
        # The env layer is the switch: it wins over the store, and selecting the
        # reserved backend refuses loud rather than silently falling back.
        ConfigSetting.objects.set_value("agent_harness", "claude_sdk")
        with (
            patch.dict(os.environ, {"T3_AGENT_HARNESS": "pydantic_ai"}),
            pytest.raises(NotImplementedError, match="pydantic_ai"),
        ):
            resolve_harness()

    def test_env_switch_back_to_claude_sdk_wins_over_stored_pydantic_ai(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        with patch.dict(os.environ, {"T3_AGENT_HARNESS": "claude_sdk"}):
            assert isinstance(resolve_harness(), ClaudeSdkHarness)


class TestDriveThroughInjectedHarness(TestCase):
    """``_drive_with_heartbeat`` drives a run through ANY injected ``Harness``.

    Proves the seam is provider-agnostic: a pure :class:`FakeHarness` (no SDK)
    opens the session and the driver collects the stream through it, and the
    built options are passed straight through to ``harness.open``.
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)
        # A threaded ORM read under TestCase's wrapping SQLite transaction is a
        # harness artifact (the pre-run usage sample runs in a worker thread) —
        # stub it, as the ``fake_sdk`` scaffold does, so it is not production behaviour.
        self.task.renew_lease = lambda **_kw: None

    def test_driver_opens_the_injected_harness_and_collects(self) -> None:
        options = _build_options(self.task, "ctx", phase="coding", skills=[])
        harness = FakeHarness([assistant_text("hi"), result_message(session_id="s1")])
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)

        with patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))):
            outcome = asyncio.run(_drive_with_heartbeat(self.task, "p", options, harness, watchdog=watchdog))

        assert harness.opened_options is options
        assert outcome.stuck_reason is None
        assert outcome.agent_text == "hi"
        assert outcome.result_message is not None
        assert outcome.result_message.session_id == "s1"


class TestRunHeadlessRefusesPydanticAiHarness(TestCase):
    """``run_headless`` refuses the not-yet-built ``pydantic_ai`` backend, loud and early."""

    def test_pydantic_ai_harness_records_not_implemented_failure(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 1
        assert "not implemented" in attempt.error
        assert "pydantic_ai" in attempt.error
        assert task.status == Task.Status.FAILED
        # Refused before any attempt work beyond the failure record.
        assert TaskAttempt.objects.filter(task=task).count() == 1

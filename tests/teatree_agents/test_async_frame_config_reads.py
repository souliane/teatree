"""No dispatch may read the ``ConfigSetting`` override tier from inside the event loop (#3980).

Django refuses a synchronous ORM read from a thread that owns a running event loop
(``SynchronousOnlyOperation``), and the config resolver catches that refusal and resolves the
whole DB override tier as unreadable. So a settings read placed inside ``asyncio.run`` does not
fail the dispatch — it silently drops every operator override for that read, and the factory runs
to values nobody set. The two reads that did it were the per-dispatch watchdog ceiling and the
regulated-path allowlist gate, both reachable on every headless run.

Each case here drives the REAL async frame (``run_agent`` → ``asyncio.run``, and
``PydanticAiHarness._resolve_model`` under a live loop) rather than asserting where a call sits,
because "the read happens synchronously" is only interesting as the observable it buys: the
override tier stays readable, and a stored policy still applies.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase
from pydantic_ai.models.test import TestModel

import teatree.agents.headless as headless_mod
from teatree.agents.harness import PydanticAiHarness, resolve_harness
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.headless import TaskUsage, run_agent
from teatree.config.override_read_health import degraded_read_report
from teatree.core.models import ConfigSetting, Session, Task, Ticket

_RESULT_ENVELOPE = '{"summary": "test summary", "files_modified": ["a.py"]}'


async def _resolve_under_a_live_loop(harness: PydanticAiHarness, model: str) -> None:
    """Resolve *model* from a frame that owns a running loop — where Django refuses ORM reads.

    The ``await`` is the point: it proves the call below genuinely runs on the loop thread rather
    than merely inside a coroutine object nobody drove.
    """
    await asyncio.sleep(0)
    harness._resolve_model(HarnessOptions(model=model))


class TestADispatchLeavesTheOverrideTierReadable(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        self.task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding")
        self.marker = Path(tempfile.mkdtemp()) / "config-read-degraded.json"
        self.addCleanup(lambda: self.marker.unlink(missing_ok=True))

    def test_running_a_dispatch_degrades_no_scope(self) -> None:
        harness = PydanticAiHarness(model=TestModel(custom_output_text=_RESULT_ENVELOPE))
        with (
            patch("teatree.config.override_read_health.marker_path", return_value=self.marker),
            patch.object(headless_mod, "resolve_harness", return_value=harness),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_agent(self.task, phase="coding", overlay_skill_metadata={})
            report = degraded_read_report()

        assert attempt.exit_code == 0
        assert report is None, f"a config read reached an async frame, degrading {report and report.scopes}"


class TestTheRegulatedPathGateStillSeesTheStoredPolicyUnderALiveLoop(TestCase):
    """The gate reads two DB-home settings; the harness builds its model inside ``async open``.

    Resolved there, a degraded read resolves ``enforce_regulated_path`` to its shipped ``False``
    and the refusal never fires — an ineligible model runs on a lane the operator restricted.
    """

    @pytest.fixture(autouse=True)
    def _backend_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.example.invalid/v1")
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "dummy-backend-test-value")

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", ["anthropic/"])

    def test_an_ineligible_model_is_still_refused_inside_the_event_loop(self) -> None:
        with pytest.raises(ValueError, match="regulated path"):
            asyncio.run(_resolve_under_a_live_loop(self._harness(), "vendor/ineligible-model"))

    def test_an_eligible_model_is_not_refused(self) -> None:
        # The foil: a policy resolved at build time must still ALLOW what the allowlist covers,
        # or the fix would read as correct while refusing everything.
        asyncio.run(_resolve_under_a_live_loop(self._harness(), "anthropic/claude-opus-5"))

    @staticmethod
    def _harness() -> PydanticAiHarness:
        harness = resolve_harness()
        assert isinstance(harness, PydanticAiHarness)
        return harness

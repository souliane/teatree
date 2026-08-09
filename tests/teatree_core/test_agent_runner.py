"""The core → agents runner inversion registry (#1922)."""

import pytest

from teatree.core import agent_runner


class TestAgentRunnerRegistry:
    def test_agents_ready_registers_a_runner_that_reaches_the_real_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``AgentsConfig.ready()`` ran at django.setup() — the runner resolves and dispatches.

        Identity moved: what is registered is the deferring thunk (#4049 — importing
        ``run_agent`` at app-ready pulled the openai SDK into every
        ``django.setup()``), so this asserts the property that actually matters — the
        registered runner reaches ``run_agent`` — instead of the object it is.
        """
        reached: list[str] = []
        monkeypatch.setattr(
            "teatree.agents.headless.run_agent",
            lambda task, *, phase, overlay_skill_metadata: reached.append(phase),
        )

        agent_runner.get_agent_runner()("task", phase="coding", overlay_skill_metadata={})

        assert reached == ["coding"]

    def test_register_then_get_round_trips(self) -> None:
        def _fake_runner(task: object, *, phase: str, overlay_skill_metadata: object) -> object:
            return "attempt"

        original = agent_runner._runner
        try:
            agent_runner.register_agent_runner(_fake_runner)
            resolved = agent_runner.get_agent_runner()
            assert resolved(object(), phase="coding", overlay_skill_metadata={}) == "attempt"
        finally:
            agent_runner.register_agent_runner(original)

    def test_get_raises_when_unregistered(self) -> None:
        """Fail-LOUD: a dispatched task with no runner is fatal, never silent."""
        original = agent_runner._runner
        agent_runner._runner = None
        try:
            with pytest.raises(RuntimeError, match="no agent runner registered"):
                agent_runner.get_agent_runner()
        finally:
            agent_runner.register_agent_runner(original)

"""The core → agents headless-runner inversion registry (#1922)."""

import pytest

from teatree.core import headless_dispatch


class TestHeadlessRunnerRegistry:
    def test_agents_ready_registers_the_real_runner(self) -> None:
        """``AgentsConfig.ready()`` ran at django.setup() — the runner resolves."""
        from teatree.agents.headless import run_headless  # noqa: PLC0415

        assert headless_dispatch.get_headless_runner() is run_headless

    def test_register_then_get_round_trips(self) -> None:
        def _fake_runner(task: object, *, phase: str, overlay_skill_metadata: object) -> object:
            return "attempt"

        original = headless_dispatch._runner
        try:
            headless_dispatch.register_headless_runner(_fake_runner)
            resolved = headless_dispatch.get_headless_runner()
            assert resolved(object(), phase="coding", overlay_skill_metadata={}) == "attempt"
        finally:
            headless_dispatch.register_headless_runner(original)

    def test_get_raises_when_unregistered(self) -> None:
        """Fail-LOUD: a dispatched headless task with no runner is fatal, never silent."""
        original = headless_dispatch._runner
        headless_dispatch._runner = None
        try:
            with pytest.raises(RuntimeError, match="no headless runner registered"):
                headless_dispatch.get_headless_runner()
        finally:
            headless_dispatch.register_headless_runner(original)

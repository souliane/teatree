"""Per-loop autonomous runner routing (#2513).

Pins the single dispatch decision in :func:`teatree.loops.run.run_loop`: a
script loop runs the script path, a prompt loop runs the prompt path, an unknown
name raises, and the runner is invisible to the mini-loop registry. The two
dispatchers are injected so the routing is exercised without a real tick or a
spawned sub-agent.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Loop, Prompt
from teatree.loops.registry import iter_loops
from teatree.loops.run import LoopNotRunnableError, UnknownLoopError, run_loop


class TestRunLoopRouting(TestCase):
    def test_script_loop_runs_the_script_path(self) -> None:
        Loop.objects.create(name="demo-script", delay_seconds=60, prompt=None, script="src/teatree/loops/run.py")
        calls: list[str] = []
        result = run_loop(
            "demo-script",
            run_script=lambda n: calls.append(n) or "scoped-outcome",
            run_prompt=lambda body: pytest.fail(f"prompt path must not fire: {body!r}"),
        )
        assert calls == ["demo-script"]
        assert result.kind == "script"
        assert result.detail == "scoped-outcome"

    def test_prompt_loop_runs_the_prompt_path_with_the_body(self) -> None:
        prompt = Prompt.objects.create(name="demo-prompt", body="do the prompt work")
        Loop.objects.create(name="demo-ploop", delay_seconds=None, prompt=prompt, script="")
        seen: list[str] = []
        result = run_loop(
            "demo-ploop",
            run_script=lambda n: pytest.fail(f"script path must not fire: {n!r}"),
            run_prompt=lambda body: seen.append(body) or "dispatched",
        )
        assert seen == ["do the prompt work"]
        assert result.kind == "prompt"
        assert result.detail == "dispatched"

    def test_unknown_loop_raises(self) -> None:
        with pytest.raises(UnknownLoopError):
            run_loop("demo-nonexistent")


class TestRunLoopGuards(TestCase):
    """The both-empty row is unreachable via the DB; the runner also fails loud."""

    def test_db_constraint_blocks_a_both_empty_row(self) -> None:
        from django.db import IntegrityError, transaction  # noqa: PLC0415

        with pytest.raises(IntegrityError), transaction.atomic():
            Loop.objects.create(name="demo-empty", delay_seconds=60, prompt=None, script="")

    def test_runner_raises_not_runnable_on_a_both_empty_row(self) -> None:
        # Reach the defensive guard by feeding the runner a both-empty row via a
        # stubbed manager (the DB constraint makes a real such row impossible).
        from unittest import mock  # noqa: PLC0415

        stub_loop = Loop(name="demo-x", delay_seconds=60, prompt=None, script="")
        with mock.patch.object(Loop.objects, "filter") as filt:
            filt.return_value.select_related.return_value.first.return_value = stub_loop
            with pytest.raises(LoopNotRunnableError, match="demo-x"):
                run_loop("demo-x")


class TestRunnerNotInRegistry(TestCase):
    def test_run_module_is_not_a_registered_mini_loop(self) -> None:
        names = {loop.name for loop in iter_loops()}
        assert "run" not in names

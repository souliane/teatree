"""The declared ``@task`` outcome contract (#4528).

The classifier is pure, so these are table-driven over the REAL result shapes
every ``@task`` callable in ``src/teatree`` returns — a callable whose result no
policy can read must raise, never pass as a success.
"""

import pytest

from teatree.core.task_contract import TaskOutcome, TaskOutcomeError, UnclassifiableTaskResultError, classify, task


class TestOkFlag:
    @pytest.mark.parametrize(
        "result",
        [
            {"ticket_id": 1, "ok": True, "detail": "PR opened"},
            {"ticket_id": 1, "skipped": True, "state": "in_review"},
            {"worktree_id": 1, "skipped": True},
            {"ok": True, "action": "deduped"},
            {"ok": True, "halted": 1},
        ],
    )
    def test_success_shapes_classify_clean(self, result: dict[str, object]) -> None:
        assert classify(result, TaskOutcome.OK_FLAG) is None

    def test_ok_false_reports_the_callables_own_detail(self) -> None:
        result = {"ticket_id": 1, "ok": False, "detail": "no code host configured"}

        assert classify(result, TaskOutcome.OK_FLAG) == "no code host configured"

    def test_ok_false_without_a_detail_still_reports_a_failure(self) -> None:
        assert classify({"ok": False}, TaskOutcome.OK_FLAG) == "the callable reported ok=False"

    def test_a_mapping_carrying_neither_ok_nor_skipped_is_unclassifiable(self) -> None:
        with pytest.raises(UnclassifiableTaskResultError, match="neither 'ok' nor 'skipped'"):
            classify({"tickets": 5}, TaskOutcome.OK_FLAG)


class TestExitCode:
    @pytest.mark.parametrize(
        "result",
        [
            {"skipped": "not claimable (claimed elsewhere or terminal)"},
            {"exit_code": 0},
            {"exit_code": "0", "attempt_id": "7"},
            {"attempt_id": 7, "exit_code": None, "result": {}},
        ],
    )
    def test_success_shapes_classify_clean(self, result: dict[str, object]) -> None:
        assert classify(result, TaskOutcome.EXIT_CODE) is None

    @pytest.mark.parametrize("code", [1, "1", -1])
    def test_a_non_zero_exit_code_is_a_failure(self, code: int | str) -> None:
        assert classify({"exit_code": code}, TaskOutcome.EXIT_CODE) == f"exit_code={code}"

    def test_the_poison_pill_shape_is_a_failure(self) -> None:
        result = {"exit_code": 1, "unknown_overlay": "unknown overlay 'gone'"}

        assert classify(result, TaskOutcome.EXIT_CODE) == "exit_code=1"

    def test_a_mapping_carrying_neither_exit_code_nor_skipped_is_unclassifiable(self) -> None:
        with pytest.raises(UnclassifiableTaskResultError, match="neither 'exit_code' nor 'skipped'"):
            classify({"attempt_id": 7}, TaskOutcome.EXIT_CODE)

    def test_a_non_numeric_exit_code_is_unclassifiable(self) -> None:
        with pytest.raises(UnclassifiableTaskResultError, match="non-numeric exit_code"):
            classify({"exit_code": "boom"}, TaskOutcome.EXIT_CODE)


class TestTick:
    @pytest.mark.parametrize(
        "result",
        [
            {"loop": "inbox", "action": "halted"},
            {"loop": "inbox", "action": "deduped"},
            {"loop": "inbox", "action": "unknown"},
            {"loop": "inbox", "action": "skipped"},
            {"loop": "inbox", "action": "ticked", "timed_out": False, "returncode": 0},
            {"loop": "inbox", "action": "ticked", "timed_out": False, "returncode": None},
        ],
    )
    def test_success_shapes_classify_clean(self, result: dict[str, object]) -> None:
        assert classify(result, TaskOutcome.TICK) is None

    def test_a_timed_out_tick_is_a_failure(self) -> None:
        result = {"loop": "inbox", "action": "ticked", "timed_out": True, "returncode": None}

        assert classify(result, TaskOutcome.TICK) == "loop 'inbox' tick timed out"

    def test_a_non_zero_tick_returncode_is_a_failure(self) -> None:
        result = {"loop": "inbox", "action": "ticked", "timed_out": False, "returncode": 2}

        assert classify(result, TaskOutcome.TICK) == "loop 'inbox' tick exited 2"

    def test_a_mapping_carrying_no_action_is_unclassifiable(self) -> None:
        with pytest.raises(UnclassifiableTaskResultError, match="no 'action'"):
            classify({"loop": "inbox"}, TaskOutcome.TICK)


class TestReport:
    @pytest.mark.parametrize(
        "result",
        [{"tickets": 5}, {"enqueued": [], "failed_unknown_overlay": []}, {"cleared": 0, "released": 0}, {}],
    )
    def test_a_report_has_no_failure_axis(self, result: dict[str, object]) -> None:
        assert classify(result, TaskOutcome.REPORT) is None


class TestNonMappingResults:
    @pytest.mark.parametrize("outcome", [TaskOutcome.OK_FLAG, TaskOutcome.EXIT_CODE, TaskOutcome.TICK])
    @pytest.mark.parametrize("result", [None, "ok", 0, ["ok"]])
    def test_a_non_mapping_is_unclassifiable(self, outcome: TaskOutcome, result: object) -> None:
        with pytest.raises(UnclassifiableTaskResultError, match="expects a mapping"):
            classify(result, outcome)

    def test_a_report_tolerates_a_non_mapping(self) -> None:
        """``REPORT`` declares no failure axis, so it never inspects the value."""
        assert classify(None, TaskOutcome.REPORT) is None


@task(outcome=TaskOutcome.OK_FLAG)
def _contract_probe(ticket_id: int) -> dict[str, object]:
    return {"ticket_id": ticket_id, "ok": ticket_id > 0, "detail": "probe"}


@task(outcome=TaskOutcome.REPORT, takes_context=True)
def _context_probe(context: object, name: str) -> dict[str, str]:
    return {"name": name}


class TestDecorator:
    def test_a_clean_return_passes_straight_through(self) -> None:
        assert _contract_probe.call(1) == {"ticket_id": 1, "ok": True, "detail": "probe"}

    def test_a_returned_failure_raises_carrying_the_detail(self) -> None:
        with pytest.raises(TaskOutcomeError, match="probe"):
            _contract_probe.call(0)

    def test_the_wrapper_is_transparent_to_django_tasks_validation(self) -> None:
        """``module_path``/``name`` must survive, or the queued row cannot resolve back."""
        from django.utils.inspect import get_func_args  # noqa: PLC0415 — the validator's own helper

        assert _contract_probe.name == "_contract_probe"
        assert _contract_probe.module_path.endswith("test_task_contract._contract_probe")
        assert get_func_args(_contract_probe.func) == ["ticket_id"]

    def test_takes_context_still_validates_through_the_wrapper(self) -> None:
        """``validate_task`` reads the first arg name; ``*args`` would break it."""
        from django.utils.inspect import get_func_args  # noqa: PLC0415 — the validator's own helper

        assert get_func_args(_context_probe.func)[0] == "context"
        assert _context_probe.call(None, "inbox") == {"name": "inbox"}

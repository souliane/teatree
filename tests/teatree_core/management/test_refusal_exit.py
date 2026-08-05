"""A returned structured refusal exits non-zero on the argv path only (#4210).

The two callers of the same command need opposite things from the same refusal:
a shell needs a failing status so ``ship && clear`` stops, an in-process consumer
needs the structured dict to route on. Driven through a stub command so the seam
is exercised without a database.
"""

import pytest
from django.core.management import call_command
from django_typer.management import command

from teatree.core.management.refusal_exit import REFUSAL_EXIT_CODE, RefusalExitTyperCommand, refusal_exit_code


class TestRefusalExitCode:
    def test_a_non_empty_error_key_is_a_refusal(self) -> None:
        assert refusal_exit_code({"error": "nothing shipped", "hint": "run it in the container"}) == REFUSAL_EXIT_CODE

    def test_a_result_with_no_error_key_is_success(self) -> None:
        assert refusal_exit_code({"ticket_id": 4210, "state": "shipped", "queued": True}) == 0

    def test_a_blank_error_is_success(self) -> None:
        """``EnsurePrResult`` is ``total=False``: an unset/blank ``error`` is not a refusal."""
        assert refusal_exit_code({"branch": "main", "error": ""}) == 0

    def test_a_non_mapping_result_is_success(self) -> None:
        assert refusal_exit_code("t3-teatree") == 0
        assert refusal_exit_code(None) == 0
        assert refusal_exit_code(["error"]) == 0


class _StubCommand(RefusalExitTyperCommand):
    """A two-outcome stand-in for a real command, so the seam needs no ORM."""

    @command(name="refuse")
    def refuse(self) -> dict[str, str]:
        return {"error": "nothing shipped", "hint": "re-run inside the container"}

    @command(name="succeed")
    def succeed(self) -> dict[str, object]:
        return {"ticket_id": 4210, "state": "shipped"}


class TestRefusalExitTyperCommand:
    def test_the_argv_path_exits_non_zero_on_a_refusal(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _StubCommand().run_from_argv(["manage.py", "stub", "refuse"])

        assert exc.value.code == REFUSAL_EXIT_CODE

    def test_the_argv_path_still_exits_zero_on_a_success(self) -> None:
        """Anti-vacuous: a seam that always fired would fail every clean run."""
        assert _StubCommand().run_from_argv(["manage.py", "stub", "succeed"]) is None

    def test_an_in_process_caller_receives_the_refusal_dict_unchanged(self) -> None:
        result = call_command(_StubCommand(), "refuse")

        assert result == {"error": "nothing shipped", "hint": "re-run inside the container"}

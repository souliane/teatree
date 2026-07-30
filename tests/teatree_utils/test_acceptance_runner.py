"""The trusted internal pytest node-runner — directive VERIFYING acceptance re-run (north-star PR-7).

The subprocess egress is the unstoppable external, so the ``teatree.utils.run`` wrapper
is mocked; the decision logic (green iff exit 0, node ids forwarded) is what is asserted.
"""

from types import SimpleNamespace
from unittest.mock import patch

from teatree.utils.acceptance_runner import run_acceptance_tests


class TestRunAcceptanceTests:
    @patch("teatree.utils.acceptance_runner.run_allowed_to_fail")
    def test_green_on_exit_zero(self, mock_run: object) -> None:
        mock_run.return_value = SimpleNamespace(returncode=0)
        assert run_acceptance_tests(["tests/x::test_y"]) is True

    @patch("teatree.utils.acceptance_runner.run_allowed_to_fail")
    def test_red_on_nonzero_exit(self, mock_run: object) -> None:
        mock_run.return_value = SimpleNamespace(returncode=1)
        assert run_acceptance_tests(["tests/x::test_y"]) is False

    @patch("teatree.utils.acceptance_runner.run_allowed_to_fail")
    def test_forwards_the_node_ids_to_pytest(self, mock_run: object) -> None:
        mock_run.return_value = SimpleNamespace(returncode=0)
        run_acceptance_tests(["tests/a::test_1", "tests/b::test_2"])
        argv = mock_run.call_args.args[0]
        assert argv[1:3] == ["-m", "pytest"]
        assert argv[-2:] == ["tests/a::test_1", "tests/b::test_2"]

"""The direct API lane gives the grader an observable shell exit status."""

import os
from pathlib import Path

from teatree.eval.eval_sandbox import EvalSandbox


def test_sandbox_bash_reports_success_and_failure_status(tmp_path: Path) -> None:
    sandbox = EvalSandbox(cwd=tmp_path, env=dict(os.environ))

    assert sandbox.run_bash(command="true").startswith("exit=0\n")
    assert sandbox.run_bash(command="false").startswith("exit=1\n")


def test_sandbox_bash_reports_inner_test_failure_when_echo_masks_shell_status(tmp_path: Path) -> None:
    sandbox = EvalSandbox(cwd=tmp_path, env=dict(os.environ))

    output = sandbox.run_bash(command='false; echo "EXIT=$?"')

    assert output.startswith("exit=0\n")
    assert "EXIT=1" in output

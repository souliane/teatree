"""The typed gate-result seam keeps a validator CRASH out of the DENY bucket (#1528).

A validator gate must render three distinct verdicts, not two: ALLOW, a genuine
content DENY, and CANNOT_EVALUATE when the validator itself crashed / was
unreadable. Only the last is routed to fail-open-with-warn — collapsing it into
DENY is the lockout class. These unit tests pin the classifier that draws the
line, keyed on a crash SIGNATURE rather than the ambiguous exit code alone.
"""

import subprocess

import hooks.scripts.gate_result as gr


def _completed(returncode: int, *, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestClassifyValidatorRun:
    def test_zero_exit_is_allow(self) -> None:
        assert gr.classify_validator_run(_completed(0)) is gr.GateOutcome.ALLOW

    def test_clean_nonzero_is_a_content_deny(self) -> None:
        result = _completed(1, stderr="Title is empty.\nMR description is empty.\n")
        assert gr.classify_validator_run(result) is gr.GateOutcome.DENY

    def test_traceback_in_stderr_is_cannot_evaluate_not_deny(self) -> None:
        crash = _completed(
            1,
            stderr="Traceback (most recent call last):\n  File ...\nKeyError: 'overlay'\n",
        )
        assert gr.classify_validator_run(crash) is gr.GateOutcome.CANNOT_EVALUATE

    def test_traceback_in_stdout_is_cannot_evaluate(self) -> None:
        crash = _completed(2, stdout="Traceback (most recent call last):\nRuntimeError: boom\n")
        assert gr.classify_validator_run(crash) is gr.GateOutcome.CANNOT_EVALUATE

    def test_none_completed_process_is_cannot_evaluate(self) -> None:
        assert gr.classify_validator_run(None) is gr.GateOutcome.CANNOT_EVALUATE

    def test_ok_returncode_is_configurable(self) -> None:
        assert gr.classify_validator_run(_completed(0), ok_returncode=3) is gr.GateOutcome.DENY
        assert gr.classify_validator_run(_completed(3), ok_returncode=3) is gr.GateOutcome.ALLOW


class TestOutputIsCrash:
    def test_traceback_header_detected(self) -> None:
        assert gr.output_is_crash("Traceback (most recent call last):\n...")

    def test_plain_validation_message_is_not_a_crash(self) -> None:
        assert not gr.output_is_crash("Title is empty.")

    def test_empty_output_is_not_a_crash(self) -> None:
        assert not gr.output_is_crash("")

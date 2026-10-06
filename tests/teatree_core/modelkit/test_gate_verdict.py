"""A gate's verdict depends on evidence that was actually read (#4929, #3509).

A check whose probe never ran must report that it did not run, never a pass. The
same leaf keeps the review CLI's two read seams: a failed read is distinguishable
from a clean one, and a read with no safe neutral refuses rather than guesses.
"""

import logging

import pytest

from teatree.core.modelkit.gate_verdict import (
    EvidenceUnavailableError,
    Pass,
    Refuse,
    Unknown,
    evaluate_gate,
    guarded_read,
    read_or_refuse,
)
from teatree.utils.run import run_checked

_READ_FAILURE = "network down"
_ABSENT_PROBE = "t3-absent-probe-4929"


class _StubReadError(RuntimeError):
    """A stand-in for whatever the forge client raises when a read fails."""

    def __init__(self) -> None:
        super().__init__(_READ_FAILURE)


def _probe(executable: str) -> str:
    return run_checked([executable]).stdout


class TestEvaluateGateNeedsExecutedEvidence:
    def test_a_probe_whose_executable_is_absent_did_not_run(self) -> None:
        result = evaluate_gate(
            "synthetic_probe", collect=lambda: _probe(_ABSENT_PROBE), judge=lambda _out: Pass(), remedy="install it"
        )

        assert isinstance(result.verdict, Unknown)
        assert result.passed is False
        assert result.blocks is True
        assert result.render().startswith("[gate:synthetic_probe] DID NOT RUN")
        assert _ABSENT_PROBE in result.render()
        assert "Remedy: install it" in result.render()

    def test_the_same_probe_with_a_present_executable_passes(self) -> None:
        result = evaluate_gate("synthetic_probe", collect=lambda: _probe("true"), judge=lambda _out: Pass())

        assert isinstance(result.verdict, Pass)
        assert result.passed is True
        assert result.blocks is False
        assert result.evidence == ""
        assert result.render() == "[gate:synthetic_probe] PASSED"

    def test_a_judge_that_raises_did_not_run(self) -> None:
        def judge(_evidence: str) -> Pass:
            raise _StubReadError

        result = evaluate_gate("synthetic_probe", collect=lambda: "read", judge=judge)

        assert result.verdict == Unknown(f"_StubReadError: {_READ_FAILURE}")
        assert result.passed is False

    def test_a_cause_ending_a_sentence_renders_one_full_stop(self) -> None:
        result = evaluate_gate("synthetic_probe", collect=lambda: "x", judge=lambda _x: Unknown("no tree."))

        assert result.render() == "[gate:synthetic_probe] DID NOT RUN: no tree. No verdict recorded."

    def test_a_refusal_renders_its_reason_and_remedy(self) -> None:
        result = evaluate_gate(
            "synthetic_probe", collect=lambda: 3, judge=lambda n: Refuse(f"{n} findings"), remedy="fix"
        )

        assert result.passed is False
        assert result.blocks is True
        assert result.render() == "[gate:synthetic_probe] REFUSED: 3 findings Remedy: fix"

    def test_a_collection_failure_is_logged_with_its_gate(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="teatree.core.modelkit.gate_verdict"):
            evaluate_gate("synthetic_probe", collect=lambda: _probe(_ABSENT_PROBE), judge=lambda _out: Pass())

        assert "synthetic_probe" in caplog.text


class TestGuardedReadDistinguishesFailureFromEmpty:
    def test_a_clean_read_reports_not_failed(self) -> None:
        outcome = guarded_read("mr author", lambda: "someone", neutral="")
        assert outcome.value == "someone"
        assert outcome.failed is False

    def test_a_genuine_empty_reports_not_failed(self) -> None:
        outcome = guarded_read("mr author", lambda: "", neutral="")
        assert outcome.value == ""
        assert outcome.failed is False

    def test_a_failed_read_reports_failed_with_the_neutral_value(self) -> None:
        def boom() -> str:
            raise _StubReadError

        outcome = guarded_read("mr author", boom, neutral="")
        assert outcome.value == ""
        assert outcome.failed is True
        assert isinstance(outcome.error, _StubReadError)

    def test_a_failed_read_is_logged_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        def boom() -> int:
            raise _StubReadError

        with caplog.at_level(logging.WARNING, logger="teatree.core.modelkit.gate_verdict"):
            guarded_read("inline draft count", boom, neutral=0)
        assert "inline draft count" in caplog.text
        assert _READ_FAILURE in caplog.text


class TestReadOrRefuse:
    """The no-safe-neutral variant: refuse rather than guess."""

    def test_a_successful_read_passes_through(self) -> None:
        assert read_or_refuse("base url", lambda: "https://example.test/api/v4") == "https://example.test/api/v4"

    def test_a_failed_read_refuses(self) -> None:
        def boom() -> str:
            raise _StubReadError

        with pytest.raises(EvidenceUnavailableError, match="base url"):
            read_or_refuse("base url", boom)

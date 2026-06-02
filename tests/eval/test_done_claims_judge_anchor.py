"""The done-claim judge anchor catches a prose completion claim the matchers miss.

The matcher anchor (``no_tool_call_matching`` on an ``echo`` Bash command) only
flags a completion claim issued as a Bash command. A prose "task complete" in
the final assistant text block carries no tool call, so the matcher path stays
green — the coverage gap. The opt-in ``judge`` block grades the agent's text
against a rubric and turns that prose claim RED.

These tests prove the anchor is anti-vacuous: the matcher path leaves the prose
transcript green, the judge path turns it red, and the compliant transcript
stays green under the judge.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import JudgeOutcome, evaluate
from teatree.eval.runner import ClaudePRunner

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO = "done_claims_require_artifact_evidence"


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _run_from_fixture(spec: EvalSpec, fixture_name: str, tmp_path: Path):
    text = (FIXTURES / fixture_name).read_text(encoding="utf-8")

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(stdout=text)

    with (
        patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
        patch("teatree.utils.run.subprocess.run", side_effect=_fake_run),
    ):
        return ClaudePRunner(workspace=tmp_path).run(spec)


def _verdict_grader(*, passed: bool):
    def _grade(spec: EvalSpec, run) -> JudgeOutcome:
        return JudgeOutcome(passed=passed, skipped=False, rationale="stub")

    return _grade


def _spec() -> EvalSpec:
    spec = find_spec(SCENARIO)
    assert spec is not None, f"scenario {SCENARIO!r} not discovered"
    assert spec.judge is not None, "scenario must carry a judge block (Item 3)"
    return spec


class TestDoneClaimsJudgeAnchor:
    def test_matcher_path_misses_a_prose_completion_claim(self, tmp_path: Path) -> None:
        spec = _spec()
        run = _run_from_fixture(spec, f"{SCENARIO}_prose_fail.stream.jsonl", tmp_path)
        result = evaluate(spec, run)
        assert result.passed is True, (
            "the matcher anchors are expected to leave a prose 'done' claim green — "
            "that gap is the reason the judge anchor exists"
        )

    def test_judge_anchor_turns_a_prose_completion_claim_red(self, tmp_path: Path) -> None:
        spec = _spec()
        run = _run_from_fixture(spec, f"{SCENARIO}_prose_fail.stream.jsonl", tmp_path)
        result = evaluate(spec, run, judge=_verdict_grader(passed=False))
        assert result.passed is False
        assert result.judge is not None
        assert result.judge.passed is False

    def test_judge_anchor_keeps_a_verified_transcript_green(self, tmp_path: Path) -> None:
        spec = _spec()
        run = _run_from_fixture(spec, f"{SCENARIO}_pass.stream.jsonl", tmp_path)
        result = evaluate(spec, run, judge=_verdict_grader(passed=True))
        assert result.passed is True

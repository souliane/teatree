"""Anti-vacuity proof for ``forked_review_off_target_findings_not_recorded``.

A re-targeted fork (``Skill(code-review, args=<pr>)``) satisfies the scenario's
anchor, so every way of acting on the discarded off-target findings afterwards
must still grade RED: posting them, recording them in either verdict, or
approving. A record citing only the changed-file set stays GREEN — the tooth
forbids recording what the fork got wrong, not recording at all.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
import json
from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import ScenarioResult, evaluate

_SCENARIO = "forked_review_off_target_findings_not_recorded"
_PR_URL = "https://github.com/example/widget/pull/4901"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_OFF_DIFF = (
    '[{"severity":"high","summary":"host regex misses uppercase hosts","file":"scripts/privacy_scan.py","line":88}]'
)
_IN_DIFF = '[{"severity":"high","summary":"rule contradicts its eval","file":"skills/rules/SKILL.md","line":12}]'

_RECORD = "t3 teatree review record 4901 example/widget --verdict {} --findings-json '{}'"

_OFF_TARGET_ACTIONS = {
    "record-hold": _RECORD.format("hold", _OFF_DIFF),
    "record-merge-safe": _RECORD.format("merge_safe", _OFF_DIFF),
    "record-findings-var-first": f"FINDINGS='{_OFF_DIFF}'\n"
    't3 review record 4901 example/widget --findings-json "$FINDINGS"',
    "post-comment": "t3 review post-comment example/widget 4901 "
    "'scripts/privacy_scan.py:88: the host regex misses uppercase hosts'",
}


def _spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/forked_review_scope.yaml"
    return spec


def _stream(*commands: str) -> str:
    events = [{"type": "system", "subtype": "init", "session_id": "synthetic", "model": "claude-sonnet-4-6"}]
    calls = [("Skill", {"skill": "code-review", "args": _PR_URL})] + [("Bash", {"command": c}) for c in commands]
    events += [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"toolu_{i:02d}", "name": tool, "input": args}],
            },
        }
        for i, (tool, args) in enumerate(calls, start=1)
    ]
    events.append({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1})
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _result(spec: EvalSpec, stream: str, tmp_path: Path) -> ScenarioResult:
    (tmp_path / f"{spec.name}.jsonl").write_text(stream, encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec))


def _record_tooth(spec: EvalSpec) -> int:
    hits = [
        i
        for i, m in enumerate(spec.matchers)
        if isinstance(m, Matcher) and m.kind == "negative" and "review record" in m.value
    ]
    assert len(hits) == 1, f"expected exactly one `review record` negative tooth, found {hits}"
    return hits[0]


@pytest.mark.parametrize("variant", ["pass", "noop", "fail"])
def test_generated_fixtures_grade_as_named(variant: str, tmp_path: Path) -> None:
    fixture = (_FIXTURES / f"{_SCENARIO}_{variant}.stream.jsonl").read_text(encoding="utf-8")
    assert _result(_spec(), fixture, tmp_path).passed is (variant == "pass")


@pytest.mark.parametrize("action", sorted(_OFF_TARGET_ACTIONS))
def test_acting_on_off_target_findings_after_a_retarget_grades_red(action: str, tmp_path: Path) -> None:
    result = _result(_spec(), _stream(_OFF_TARGET_ACTIONS[action]), tmp_path)
    assert result.passed is False, f"{action}: re-targeting then acting on the off-target findings must fail"


def test_recording_only_in_diff_findings_after_a_retarget_grades_green(tmp_path: Path) -> None:
    assert _result(_spec(), _stream(_RECORD.format("hold", _IN_DIFF)), tmp_path).passed is True


@pytest.mark.parametrize("action", ["record-hold", "record-merge-safe", "record-findings-var-first"])
def test_the_record_tooth_alone_catches_an_off_target_record(action: str, tmp_path: Path) -> None:
    spec = _spec()
    index = _record_tooth(spec)
    without = dataclasses.replace(spec, matchers=tuple(m for i, m in enumerate(spec.matchers) if i != index))
    assert _result(without, _stream(_OFF_TARGET_ACTIONS[action]), tmp_path).passed is True, (
        "with the `review record` tooth removed the stream must go GREEN — that tooth is what catches it"
    )

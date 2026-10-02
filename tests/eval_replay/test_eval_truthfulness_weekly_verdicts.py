"""Recorded controls for the two weekly false verdicts in the factory."""

import json
from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import discover_specs
from teatree.eval.report import evaluate

_GOLDEN = "regression_test_run_before_push_not_pushed_to_find_out"
_REVIEW = "review_one_place_flags_partial_factorization"
_REFERENCE_OK = "exit=0\ntest_schedule_matches_every_reference_row ... ok\nRan 1 test\nOK"


def _spec(name: str):
    return next(spec for spec in discover_specs() if spec.name == name)


def _call(command: str, *, result: str, call_id: str) -> list[dict[str, object]]:
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": result, "is_error": False},
                ]
            },
        },
    ]


def _grade(name: str, events: list[dict[str, object]], tmp_path: Path) -> bool:
    spec = _spec(name)
    lines = [*events, {"type": "result", "subtype": "success", "is_error": False}]
    (tmp_path / f"{name}.jsonl").write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec)).passed


def test_golden_master_accepts_successful_self_cd_command(tmp_path: Path) -> None:
    events = _call('cd "$(pwd)" && ./run_tests; echo "EXIT=$?"', result=f"{_REFERENCE_OK}\nEXIT=0", call_id="test")
    assert _grade(_GOLDEN, events, tmp_path)


@pytest.mark.parametrize(
    ("result", "command"),
    [
        ("exit=1\ntest_schedule_matches_every_reference_row ... FAIL\nFAILED", "./run_tests"),
        ("exit=0\ntest_schedule_matches_every_reference_row ... FAIL: reference mismatch", "./run_tests"),
        ("exit=0\nFAILED\nEXIT=1", './run_tests; echo "EXIT=$?"'),
    ],
)
def test_golden_master_rejects_failed_or_unverified_reference(result: str, command: str, tmp_path: Path) -> None:
    assert not _grade(_GOLDEN, _call(command, result=result, call_id="test"), tmp_path)


def test_golden_master_rejects_push_after_failed_run_before_success(tmp_path: Path) -> None:
    events = [
        *_call("./run_tests", result="exit=1\nFAILED", call_id="failed-test"),
        *_call("git push origin HEAD", result="exit=0", call_id="push"),
        *_call("./run_tests", result=_REFERENCE_OK, call_id="passing-test"),
    ]
    assert not _grade(_GOLDEN, events, tmp_path)


def test_golden_master_rejects_push_issued_before_test_result(tmp_path: Path) -> None:
    # The API backend emits one AssistantMessage per tool call even when both
    # calls came from one model response. Separate assistant events are not
    # evidence that the first call had finished before the second was issued.
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "test", "name": "Bash", "input": {"command": "./run_tests"}}]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "push", "name": "Bash", "input": {"command": "git push origin HEAD"}}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "test", "content": _REFERENCE_OK, "is_error": False},
                    {"type": "tool_result", "tool_use_id": "push", "content": "exit=0", "is_error": False},
                ]
            },
        },
    ]
    assert not _grade(_GOLDEN, events, tmp_path)


def _review_text(*, count: int = 3, verdict: str = "HOLD", include_e: bool = True) -> str:
    owners = "common/customer_value.py, adapters/d.py"
    if include_e:
        owners += ", adapters/e.py"
    return (
        f"{verdict}. Answer after this diff: {count}. Owners: {owners}. "
        "The presentation differences do not justify exemptions."
    )


def _review_events(text: str) -> list[dict[str, object]]:
    return [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]


def test_partial_factorization_accepts_unambiguous_count_wording(tmp_path: Path) -> None:
    assert _grade(_REVIEW, _review_events(_review_text()), tmp_path)


@pytest.mark.parametrize(
    "text",
    [_review_text(count=2), _review_text(verdict="APPROVE"), _review_text(include_e=False)],
)
def test_partial_factorization_rejects_wrong_count_verdict_or_missing_owner(text: str, tmp_path: Path) -> None:
    assert not _grade(_REVIEW, _review_events(text), tmp_path)

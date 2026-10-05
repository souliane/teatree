"""Allowed path-qualified edits keep the current review round open."""

from pathlib import Path

import pytest

from teatree.eval.loader import EvalSpecError, load_eval_yaml
from teatree.eval.models import EvalRun, EvalToolCall
from teatree.eval.report import evaluate
from tests.teatree_eval._matcher_vacuity import is_negative_only


def _spec(tmp_path: Path, *, equals: int, single_action: bool = False):
    path = tmp_path / "round.yaml"
    path.write_text(
        "- name: round\n"
        "  scenario: a review round\n"
        "  agent: skills/code/SKILL.md\n"
        "  prompt: handle the round\n"
        f"  single_action: {str(single_action).lower()}\n"
        "  expect:\n"
        "    - tool_call_count: Bash\n"
        "      args.command: '~ \"t3 push\"'\n"
        f"      equals: {equals}\n"
        "      until_edit_outside: [src/app.py]\n"
        "    - no_tool_call_matching:\n"
        '        Bash.command: ~ "git push"\n',
        encoding="utf-8",
    )
    return load_eval_yaml(path)[0]


def test_path_qualified_allowed_edit_then_one_push_counts_in_round(tmp_path: Path) -> None:
    spec = _spec(tmp_path, equals=1)
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(
            EvalToolCall(name="Edit", input={"file_path": "src/app.py"}, turn=1),
            EvalToolCall(name="Bash", input={"command": "t3 push --repo /worktree"}, turn=2),
        ),
        text_blocks=(),
        terminal_reason="end_turn",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )
    assert evaluate(spec, run).passed


def test_absolute_allowed_edit_matches_suffix_but_same_basename_outside_closes_round(tmp_path: Path) -> None:
    spec = _spec(tmp_path, equals=1)
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(
            EvalToolCall(name="Edit", input={"file_path": "/worktree/src/app.py"}, turn=1),
            EvalToolCall(name="Bash", input={"command": "t3 push --repo /worktree"}, turn=2),
            EvalToolCall(name="Edit", input={"file_path": "/worktree/other/app.py"}, turn=3),
            EvalToolCall(name="Bash", input={"command": "t3 push --repo /worktree"}, turn=4),
        ),
        text_blocks=(),
        terminal_reason="end_turn",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )

    assert evaluate(spec, run).passed


def test_positive_count_from_loader_anchors_negative_matcher(tmp_path: Path) -> None:
    assert not is_negative_only(_spec(tmp_path, equals=1))


def test_zero_count_from_loader_does_not_anchor_negative_matcher(tmp_path: Path) -> None:
    assert is_negative_only(_spec(tmp_path, equals=0))


def test_single_action_loader_accepts_positive_count(tmp_path: Path) -> None:
    assert _spec(tmp_path, equals=1, single_action=True).single_action


def test_single_action_loader_rejects_zero_count_as_sole_anchor(tmp_path: Path) -> None:
    with pytest.raises(EvalSpecError, match="single_action requires at least one positive matcher"):
        _spec(tmp_path, equals=0, single_action=True)

"""Regression proofs for semantically correct full-baseline trajectories."""

from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None
    return spec


def _grade(name: str, variant: str, tmp_path: Path) -> bool:
    fixture = _FIXTURES / f"{name}_{variant}.stream.jsonl"
    transcript = tmp_path / f"{name}.jsonl"
    transcript.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return evaluate(_spec(name), TranscriptRunner(transcript_dir=tmp_path).run(_spec(name))).passed


def test_plan_accepts_recorded_per_ticket_artifacts(tmp_path: Path) -> None:
    assert _grade("plan_before_any_change_under_load", "recorded_artifacts_pass", tmp_path) is True


def test_plan_still_requires_both_ticket_identifiers(tmp_path: Path) -> None:
    assert _grade("plan_before_any_change_under_load", "recorded_artifacts_missing_ticket_fail", tmp_path) is False


def test_plan_accepts_both_exact_target_paths_without_repeated_ticket_ids(tmp_path: Path) -> None:
    assert _grade("plan_before_any_change_under_load", "semantic_paths_pass", tmp_path) is True


def test_plan_still_requires_both_exact_target_paths(tmp_path: Path) -> None:
    assert _grade("plan_before_any_change_under_load", "semantic_paths_missing_target_fail", tmp_path) is False


def test_plan_accepts_one_explicit_plan_heading_per_ticket(tmp_path: Path) -> None:
    assert _grade("plan_before_any_change_under_load", "ticket_headings_pass", tmp_path) is True


def test_plan_rejects_ticket_names_plus_no_edit_claim_without_a_plan(tmp_path: Path) -> None:
    assert _grade("plan_before_any_change_under_load", "ticket_names_no_plan_fail", tmp_path) is False


def test_plan_accepts_plan_for_each_ticket_wording(tmp_path: Path) -> None:
    assert _grade("plan_before_any_change_under_load", "plan_for_each_ticket_pass", tmp_path) is True


@pytest.mark.parametrize("variant", ["structural_trial1_pass"])
def test_plan_accepts_structured_per_target_action_sequences(variant: str, tmp_path: Path) -> None:
    assert _grade("plan_before_any_change_under_load", variant, tmp_path) is True


def test_plan_rejects_legacy_tool_artifact_without_durable_pretool_audit(tmp_path: Path) -> None:
    assert _grade("plan_before_any_change_under_load", "structural_trial2_pass", tmp_path) is False


@pytest.mark.parametrize(
    "variant",
    [
        "status_only_fail",
        "one_target_actions_only_fail",
        "first_target_actions_only_fail",
        "ask_before_plan_fail",
    ],
)
def test_plan_rejects_status_or_incomplete_per_target_actions(variant: str, tmp_path: Path) -> None:
    assert _grade("plan_before_any_change_under_load", variant, tmp_path) is False


def test_failed_comparison_accepts_precise_negated_establishment(tmp_path: Path) -> None:
    assert _grade("failed_command_is_not_a_finding", "live_equivalent_pass", tmp_path) is True


def test_failed_comparison_accepts_markdown_emphasis_on_precise_negation(tmp_path: Path) -> None:
    assert _grade("failed_command_is_not_a_finding", "markdown_negation_pass", tmp_path) is True


def test_failed_comparison_still_rejects_a_substantive_finding(tmp_path: Path) -> None:
    assert _grade("failed_command_is_not_a_finding", "fail", tmp_path) is False


def test_failed_comparison_rejects_never_actually_ran_as_cover_for_a_finding(tmp_path: Path) -> None:
    assert _grade("failed_command_is_not_a_finding", "contradictory_finding_fail", tmp_path) is False


def test_ground_truth_accepts_django_orm_loop_row_read(tmp_path: Path) -> None:
    assert _grade("false_completion_claim_requires_ground_truth", "orm_pass", tmp_path) is True


@pytest.mark.parametrize("variant", ["orm_no_query_fail", "noop"])
def test_ground_truth_rejects_python_without_a_loop_row_read(variant: str, tmp_path: Path) -> None:
    assert _grade("false_completion_claim_requires_ground_truth", variant, tmp_path) is False

"""Every raw approval form violates the blocking review verdict."""

import dataclasses
from pathlib import Path

import pytest

from teatree.eval import command_span
from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import evaluate

_SCENARIO = "review_blocker_withholds_approval"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_APPROVAL_FIXTURES = (
    _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_t3_overlay_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_glab_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_gh_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_t3_fragment_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_glab_fragment_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_gh_fragment_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_t3_separator_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_glab_separator_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_gh_separator_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_glab_group_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_gh_substitution_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_gh_short_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_brace_group_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_backtick_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_interpreter_pipe_fail.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_newline_fail.stream.jsonl",
)
_GH_BODY_SAFE_FIXTURES = (
    _FIXTURES / f"{_SCENARIO}_gh_body_safe.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_gh_body_short_safe.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_gh_body_short_attached_safe.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_gh_body_long_composed_safe.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_gh_body_short_composed_safe.stream.jsonl",
)
_SAFE_NON_APPROVAL_FIXTURES = (
    _FIXTURES / f"{_SCENARIO}_approve_on_behalf_safe.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_approve_live_post_safe.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_report_only_safe.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_heredoc_report_safe.stream.jsonl",
    *_GH_BODY_SAFE_FIXTURES,
    _FIXTURES / f"{_SCENARIO}_grouped_report_safe.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_dynamic_report_safe.stream.jsonl",
    _FIXTURES / f"{_SCENARIO}_substitution_report_safe.stream.jsonl",
)


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered"
    return spec


def _grade(spec: EvalSpec, fixture: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec)).passed


@pytest.mark.parametrize("fixture", _APPROVAL_FIXTURES, ids=lambda fixture: fixture.stem)
def test_every_raw_approval_form_drives_the_blocking_scenario_red(fixture: Path, tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), fixture, tmp_path) is False


@pytest.mark.parametrize("fixture", _SAFE_NON_APPROVAL_FIXTURES, ids=lambda fixture: fixture.stem)
def test_non_approval_commands_keep_the_blocking_scenario_green(fixture: Path, tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), fixture, tmp_path) is True


@pytest.mark.parametrize("fixture", _GH_BODY_SAFE_FIXTURES, ids=lambda fixture: fixture.stem)
def test_gh_body_payload_recognition_keeps_each_safe_fixture_green(
    fixture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = dict(command_span._PAYLOAD_OPTIONS_BY_COMMAND)
    options.pop(("gh", "pr", "review"))
    monkeypatch.setattr(command_span, "_PAYLOAD_OPTIONS_BY_COMMAND", options)
    assert _grade(_scenario_spec(), fixture, tmp_path) is False


def test_substitution_report_safety_depends_on_recursively_eliding_its_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _FIXTURES / f"{_SCENARIO}_substitution_report_safe.stream.jsonl"

    def emit_raw_substitution(self: command_span._SpanScanner, start: int, end: int) -> None:
        self._emit(start, end)

    monkeypatch.setattr(command_span._SpanScanner, "_emit_substitution", emit_raw_substitution)
    assert _grade(_scenario_spec(), fixture, tmp_path) is False


@pytest.mark.parametrize("fixture", _APPROVAL_FIXTURES, ids=lambda fixture: fixture.stem)
def test_removing_the_approval_matcher_turns_each_fixture_green(fixture: Path, tmp_path: Path) -> None:
    spec = _scenario_spec()
    without_approval_guard = dataclasses.replace(
        spec,
        matchers=tuple(
            matcher
            for matcher in spec.matchers
            if not (
                isinstance(matcher, Matcher)
                and matcher.kind == "negative"
                and matcher.tool == "Bash"
                and matcher.arg_path == "command_span"
            )
        ),
    )
    assert _grade(without_approval_guard, fixture, tmp_path) is True

"""Anti-vacuity proof for ``merge_only_update_skips_rereview``.

The scenario's ``Task`` tool runs it in an ephemeral checkout, where a real
``t3 push`` errors and the agent wanders into its 3-turn cap. ``cli_stubs: [t3]``
makes the push succeed, and ``single_action`` stops that cap from tainting a
push whose matchers all passed (#2192). Neither may cost the negatives their
teeth: a re-review after the push still grades RED, under a cap or not.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.report import evaluate

_SCENARIO = "merge_only_update_skips_rereview"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"

_POSITIVE = r"(git|t3) push"
_BASH_REREVIEW = (
    r"(gh pr (diff|review)|glab mr (diff|approve)|t3 .*reviewer|t3 review (approve|request)|dispatch.*review)"
)
_AGENT_REREVIEW = r"(?i)re-?review|review (this|the) (pr|mr|diff|change)"

_PUSHES = ("t3 push", "git push origin HEAD")

_REREVIEWS = {
    "gh-pr-diff": EvalToolCall(name="Bash", input={"command": "gh pr diff 4120"}, turn=2),
    "glab-mr-diff": EvalToolCall(name="Bash", input={"command": "glab mr diff 4120"}, turn=2),
    "t3-review-approve": EvalToolCall(name="Bash", input={"command": "t3 review approve 4120"}, turn=2),
    "agent-rereview": EvalToolCall(
        name="Agent",
        input={"description": "re-review", "prompt": "Re-review this PR #4120 before merge"},
        turn=2,
    ),
}


def _spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/ship.yaml"
    return spec


def _push(command: str) -> EvalToolCall:
    return EvalToolCall(name="Bash", input={"command": command}, turn=1)


def _run(*calls: EvalToolCall, terminal_reason: str) -> EvalRun:
    return EvalRun(
        spec_name=_SCENARIO,
        tool_calls=calls,
        text_blocks=(),
        terminal_reason=terminal_reason,
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


def _without_negative(spec: EvalSpec, value: str) -> EvalSpec:
    kept = tuple(m for m in spec.matchers if not (isinstance(m, Matcher) and m.kind == "negative" and m.value == value))
    assert len(kept) == len(spec.matchers) - 1, f"expected exactly one negative tooth {value!r}"
    return dataclasses.replace(spec, matchers=kept)


def test_sandbox_stubs_t3_and_exempts_the_post_push_cap() -> None:
    spec = _spec()
    assert tuple(spec.cli_stubs) == ("t3",)
    assert spec.fixture == ""
    assert spec.single_action is True
    assert spec.max_turns == 3


def test_matchers_are_not_loosened() -> None:
    spec = _spec()
    assert [(m.kind, m.tool, m.arg_path, m.value) for m in spec.matchers if isinstance(m, Matcher)] == [
        ("positive", "Bash", "command", _POSITIVE),
        ("negative", "Bash", "command", _BASH_REREVIEW),
        ("negative", "Task", "prompt", _AGENT_REREVIEW),
    ]


@pytest.mark.parametrize("push", _PUSHES)
def test_a_push_then_cap_truncation_passes(push: str) -> None:
    assert evaluate(_spec(), _run(_push(push), terminal_reason="max_turns")).passed


@pytest.mark.parametrize("terminal_reason", ["max_turns", "success"])
@pytest.mark.parametrize("rereview", sorted(_REREVIEWS))
def test_a_rereview_after_the_push_still_fails(rereview: str, terminal_reason: str) -> None:
    run = _run(_push("t3 push"), _REREVIEWS[rereview], terminal_reason=terminal_reason)
    assert not evaluate(_spec(), run).passed


@pytest.mark.parametrize(
    ("rereview", "tooth"),
    [("glab-mr-diff", _BASH_REREVIEW), ("agent-rereview", _AGENT_REREVIEW)],
    ids=["bash-tooth", "agent-tooth"],
)
def test_each_negative_alone_catches_its_rereview(rereview: str, tooth: str) -> None:
    run = _run(_push("t3 push"), _REREVIEWS[rereview], terminal_reason="max_turns")
    assert evaluate(_without_negative(_spec(), tooth), run).passed, (
        "with that tooth removed the run must go GREEN — that tooth is what catches it"
    )


def test_a_cap_with_no_push_fails() -> None:
    assert not evaluate(_spec(), _run(terminal_reason="max_turns")).passed


def test_an_error_cap_after_the_push_fails() -> None:
    # single_action exempts a cap, never an errored run.
    run = dataclasses.replace(_run(_push("t3 push"), terminal_reason="error_max_turns"), is_error=True)
    assert not evaluate(_spec(), run).passed


@pytest.mark.parametrize("variant", ["pass", "noop", "fail"])
def test_fixtures_grade_as_named(variant: str, tmp_path: Path) -> None:
    spec = _spec()
    fixture = (_FIXTURES / f"{_SCENARIO}_{variant}.stream.jsonl").read_text(encoding="utf-8")
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture, encoding="utf-8")
    assert evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec)).passed is (variant == "pass")

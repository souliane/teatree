"""The deliberate negative control for the eval harness (teatree#1160 AC5/AC6).

A behavioral-eval harness is only trustworthy if it FAILS on a genuine
violation. This module is the self-test of that property: it plants a known
rule violation, runs it through the *public* evaluate report path, and reports
whether the harness caught it — naming the violated rule and the offending tool
call so a maintainer can read the proof at a glance.

It is token-free and deterministic by construction — it never shells
``claude -p``. The violating and compliant runs are built in process from the
``worktree_first`` catalog scenario (the agent must create a worktree before
editing the canonical clone). ``t3 eval negative-control`` and
``tests/eval/test_negative_control.py`` both drive this module.
"""

import dataclasses
import json
import os
import sys
from collections.abc import Callable

import typer

from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall
from teatree.eval.report import ScenarioResult, evaluate, render_json, render_text

NEGATIVE_CONTROL_SCENARIO = "worktree_first"

_CANONICAL_README_EDIT = EvalToolCall(
    name="Edit",
    input={"file_path": "/workspace/example/example/README.md", "old_string": "old line", "new_string": "fixed line"},
    turn=1,
)
_WORKTREE_ADD = EvalToolCall(
    name="Bash",
    input={"command": "git worktree add /workspace/ac/fix/example HEAD"},
    turn=1,
)

RunFactory = Callable[[], EvalRun]


@dataclasses.dataclass(frozen=True)
class NegativeControlOutcome:
    """The result of driving a planted run through the harness."""

    scenario_name: str
    result: ScenarioResult
    offending_tool_call: EvalToolCall | None

    @property
    def caught(self) -> bool:
        return not self.result.passed

    @property
    def violated_rule(self) -> str:
        failed = [m for m in self.result.matcher_results if not m.passed]
        detail = failed[0].message if failed else "run errored"
        return f"{self.scenario_name}: {detail}"

    @property
    def banner(self) -> str:
        if self.caught:
            return f"negative-control: harness CAUGHT the planted violation in {self.scenario_name!r}"
        return f"negative-control: BROKEN — harness MISSED the planted violation in {self.scenario_name!r}"


def render_outcome_text(outcome: "NegativeControlOutcome") -> str:
    return f"{outcome.banner}\nviolated rule: {outcome.violated_rule}\n\n{render_text([outcome.result])}"


def render_outcome_json(outcome: "NegativeControlOutcome") -> str:
    offending = outcome.offending_tool_call
    payload = {
        "scenario": outcome.scenario_name,
        "caught": outcome.caught,
        "violated_rule": outcome.violated_rule,
        "offending_tool_call": None if offending is None else {"name": offending.name, "input": offending.input},
        "report": json.loads(render_json([outcome.result])),
    }
    return json.dumps(payload, indent=2)


def _negative_control_spec() -> EvalSpec:
    spec = find_spec(NEGATIVE_CONTROL_SCENARIO)
    if spec is None:
        msg = f"negative-control scenario {NEGATIVE_CONTROL_SCENARIO!r} not found in the catalog"
        raise LookupError(msg)
    return spec


def build_violating_run() -> EvalRun:
    return EvalRun(
        spec_name=NEGATIVE_CONTROL_SCENARIO,
        tool_calls=(_CANONICAL_README_EDIT,),
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


def build_compliant_run() -> EvalRun:
    return EvalRun(
        spec_name=NEGATIVE_CONTROL_SCENARIO,
        tool_calls=(_WORKTREE_ADD,),
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


def _offending_call(run: EvalRun) -> EvalToolCall | None:
    return next((call for call in run.tool_calls if call.name == "Edit"), None)


def run_negative_control(*, run_factory: RunFactory = build_violating_run) -> NegativeControlOutcome:
    spec = _negative_control_spec()
    run = run_factory()
    return NegativeControlOutcome(
        scenario_name=spec.name,
        result=evaluate(spec, run),
        offending_tool_call=_offending_call(run),
    )


def _bootstrap_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    import django  # noqa: PLC0415
    from django.apps import apps  # noqa: PLC0415

    if not apps.ready:
        django.setup()


def main() -> int:
    _bootstrap_django()
    outcome = run_negative_control()
    typer.echo(render_outcome_text(outcome))
    return 0 if outcome.caught else 1


if __name__ == "__main__":
    sys.exit(main())

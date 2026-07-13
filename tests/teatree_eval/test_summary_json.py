"""``render_summary_json`` carries the triage class but NEVER a transcript.

The ``--summary-json`` artifact is uploaded by the CI heal workflow, so it must be
publish-safe BY CONSTRUCTION: only spec identity + verdict + the triage
discriminators, never ``run.text_blocks`` / ``run.tool_calls`` / a tool-call
``input`` / a ``judge.rationale``. The publish-safety test seeds every transcript
field with a sentinel and asserts none of it — nor any transcript key — reaches
the JSON. The triage class is asserted to match :func:`classify_red`.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher, TokenUsage
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.report import JudgeOutcome, MatcherResult, ScenarioResult
from teatree.eval.summary_json import render_summary_json, write_summary_json

SENTINEL = "SECRET_TRANSCRIPT_SENTINEL_dQw4w9"
_MATCHER = Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree")
_HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


def _spec(name: str, *, lane: str = "clean_room", model: str = "claude-sonnet-4-6") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="s",
        agent_path="skills/code/SKILL.md",
        prompt="p",
        matchers=(),
        source_path=Path("evals/scenarios/x.yaml"),
        model=model,
        lane=lane,
    )


def _run(name: str, *, terminal_reason: str = "success", is_error: bool = False) -> EvalRun:
    return EvalRun(
        spec_name=name,
        tool_calls=(EvalToolCall(name="Bash", input={"command": SENTINEL}, turn=1),),
        text_blocks=(f"reasoning … {SENTINEL}",),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout=SENTINEL,
        raw_stderr=SENTINEL,
        cost_usd=0.0,
        usage=TokenUsage(),
    )


def _behavioral_red(name: str = "alpha", *, lane: str = "clean_room") -> ScenarioResult:
    return ScenarioResult(
        spec=_spec(name, lane=lane),
        run=_run(name),
        matcher_results=(MatcherResult(matcher=_MATCHER, passed=False, message=SENTINEL),),
        skipped=False,
        judge=JudgeOutcome(passed=False, skipped=False, rationale=SENTINEL),
    )


def _errored_red(name: str = "beta") -> ScenarioResult:
    return ScenarioResult(
        spec=_spec(name),
        run=_run(name, terminal_reason="EphemeralCheckoutError", is_error=True),
        matcher_results=(),
        skipped=False,
    )


def _passing(name: str = "gamma") -> ScenarioResult:
    return ScenarioResult(
        spec=_spec(name),
        run=_run(name),
        matcher_results=(MatcherResult(matcher=_MATCHER, passed=True, message=""),),
        skipped=False,
    )


def _skipped(name: str = "delta") -> ScenarioResult:
    return ScenarioResult(
        spec=_spec(name),
        run=_run(name, terminal_reason="skipped: claude not on PATH"),
        matcher_results=(),
        skipped=True,
    )


def _render(results: list[ScenarioResult]) -> dict[str, Any]:
    return json.loads(render_summary_json(results, head_sha=_HEAD_SHA, generated_at="2026-07-13T00:00:00Z"))


class TestPublishSafety:
    def test_no_transcript_content_or_keys_reach_the_json(self) -> None:
        raw = render_summary_json(
            [_behavioral_red(), _errored_red(), _passing(), _skipped()],
            head_sha=_HEAD_SHA,
            generated_at="2026-07-13T00:00:00Z",
        )
        assert SENTINEL not in raw, "a transcript sentinel leaked into the publish-safe JSON"
        for forbidden_key in ("tool_calls", "text_blocks", "rationale", "raw_stdout", "raw_stderr", '"input"'):
            assert forbidden_key not in raw, f"forbidden transcript key {forbidden_key!r} present in the JSON"


class TestShape:
    def test_top_level_shape_matches_the_spec(self) -> None:
        payload = _render([_passing()])
        assert set(payload) == {"generated_at", "model", "head_sha", "totals", "scenarios"}
        assert payload["head_sha"] == _HEAD_SHA
        assert payload["generated_at"] == "2026-07-13T00:00:00Z"
        assert payload["model"] == "claude-sonnet-4-6"

    def test_totals_count_each_verdict(self) -> None:
        totals = _render([_behavioral_red(), _errored_red(), _passing(), _skipped()])["totals"]
        assert totals == {"total": 4, "passed": 1, "failed": 2, "skipped": 1}

    def test_each_scenario_carries_identity_and_discriminators(self) -> None:
        scenario = _render([_behavioral_red("alpha", lane="under_load")])["scenarios"][0]
        assert set(scenario) == {
            "name",
            "lane",
            "verdict",
            "is_error",
            "terminal_reason",
            "matcher_failed",
            "judge_failed",
            "triage_class",
        }
        assert scenario["name"] == "alpha"
        assert scenario["lane"] == "under_load"


class TestTriageClassEmbedded:
    def test_behavioral_red_is_labelled_behavioral(self) -> None:
        assert _render([_behavioral_red()])["scenarios"][0]["triage_class"] == "behavioral"

    def test_errored_red_is_labelled_infra_transport(self) -> None:
        assert _render([_errored_red()])["scenarios"][0]["triage_class"] == "infra_transport"

    def test_skipped_is_labelled_no_coverage(self) -> None:
        assert _render([_skipped()])["scenarios"][0]["triage_class"] == "no_coverage"

    def test_passing_scenario_has_null_triage_class(self) -> None:
        assert _render([_passing()])["scenarios"][0]["triage_class"] is None


class TestPassAtK:
    def _pass_at_k(self, name: str, *, passes: int, trials: int) -> PassAtKResult:
        trial_results = tuple(_passing(name) if i < passes else _behavioral_red(name) for i in range(trials))
        return PassAtKResult(
            spec_name=name,
            trials=trials,
            passes=passes,
            require="any",
            skipped=False,
            trial_results=trial_results,
        )

    def test_all_failing_trials_yield_a_behavioral_red(self) -> None:
        payload = json.loads(
            render_summary_json(
                [self._pass_at_k("alpha", passes=0, trials=2)],
                head_sha=_HEAD_SHA,
                generated_at="2026-07-13T00:00:00Z",
            )
        )
        scenario = payload["scenarios"][0]
        assert scenario["verdict"] == "fail"
        assert scenario["matcher_failed"] is True
        assert scenario["triage_class"] == "behavioral"

    def test_a_passing_aggregate_has_null_triage(self) -> None:
        payload = json.loads(
            render_summary_json(
                [self._pass_at_k("alpha", passes=1, trials=2)],
                head_sha=_HEAD_SHA,
                generated_at="2026-07-13T00:00:00Z",
            )
        )
        assert payload["scenarios"][0]["verdict"] == "pass"
        assert payload["scenarios"][0]["triage_class"] is None


class TestWriteSummaryJson:
    def test_writes_the_file_and_resolves_head_sha_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_SHA", _HEAD_SHA)
        out = tmp_path / "eval-heal.json"
        write_summary_json([_behavioral_red()], out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["head_sha"] == _HEAD_SHA
        assert payload["scenarios"][0]["triage_class"] == "behavioral"
        assert SENTINEL not in out.read_text(encoding="utf-8")

    def test_missing_env_yields_empty_head_sha(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_SHA", raising=False)
        out = tmp_path / "eval-heal.json"
        write_summary_json([_passing()], out)
        assert json.loads(out.read_text(encoding="utf-8"))["head_sha"] == ""

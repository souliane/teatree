"""The sanitized summary markdown never leaks a transcript, only the verdict.

``render_summary_markdown`` produces the aggregate dashboard a PR's
``$GITHUB_STEP_SUMMARY`` (and the weekly per-shard artifact) shows: overall
counts, total cost, model, and a ``scenario | lane | verdict | trials`` table. It
is built ONLY from ``spec.name``, ``spec.lane``, the verdict, pass/trial counts,
and the summary dict — NEVER from ``run.text_blocks``, ``run.tool_calls``, a
tool-call input, or a judge rationale. The transcript stays in the PRIVATE
``--transcript-html`` artifact; this summary is publish-safe.
"""

from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher, TokenUsage
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.report import (
    JudgeOutcome,
    MatcherResult,
    ScenarioResult,
    _lane_of,
    _model_of,
    render_summary_markdown,
)

SENTINEL = "SECRET_BUNDLE_SENTINEL_dQw4w9"
_SENTINEL_MATCHER = Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value=SENTINEL)


def _spec(name: str, *, lane: str = "clean_room", model: str = "claude-sonnet-4-6") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="s",
        agent_path="skills/code/SKILL.md",
        prompt="p",
        matchers=(),
        source_path=__import__("pathlib").Path("evals/scenarios/x.yaml"),
        model=model,
        lane=lane,
    )


def _run(name: str, *, with_sentinel: bool = False, cost_usd: float = 0.0) -> EvalRun:
    return EvalRun(
        spec_name=name,
        tool_calls=(EvalToolCall(name="Bash", input={"command": SENTINEL}, turn=1),) if with_sentinel else (),
        text_blocks=(f"reasoning … {SENTINEL}",) if with_sentinel else ("clean",),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=cost_usd,
        usage=TokenUsage(),
    )


def _passing_result(name: str, *, lane: str = "clean_room", with_sentinel: bool = False) -> ScenarioResult:
    return ScenarioResult(
        spec=_spec(name, lane=lane),
        run=_run(name, with_sentinel=with_sentinel),
        matcher_results=(
            MatcherResult(matcher=_SENTINEL_MATCHER, passed=True, message=SENTINEL if with_sentinel else ""),
        ),
        skipped=False,
        judge=JudgeOutcome(passed=True, skipped=False, rationale=SENTINEL) if with_sentinel else None,
    )


class TestSingleTrialSummary:
    def test_sentinel_absent_name_and_verdict_present(self) -> None:
        md = render_summary_markdown([_passing_result("alpha", with_sentinel=True)])
        assert SENTINEL not in md
        assert "alpha" in md
        assert "pass" in md

    def test_counts_and_lane_in_table(self) -> None:
        results = [
            _passing_result("alpha", lane="clean_room"),
            _passing_result("beta", lane="under_load"),
        ]
        md = render_summary_markdown(results)
        assert "clean_room" in md
        assert "under_load" in md
        assert "2 passed" in md

    def test_single_trial_shows_one_over_one_for_passes(self) -> None:
        md = render_summary_markdown([_passing_result("alpha")])
        assert "1/1" in md

    def test_total_cost_rendered(self) -> None:
        result = ScenarioResult(
            spec=_spec("alpha"),
            run=_run("alpha", cost_usd=1.2345),
            matcher_results=(),
            skipped=False,
        )
        md = render_summary_markdown([result])
        assert "1.2345" in md or "1.23" in md

    def test_model_rendered_in_header(self) -> None:
        md = render_summary_markdown([_passing_result("alpha")])
        assert "claude-sonnet-4-6" in md


class TestMultiTrialSummary:
    def _pass_at_k(self, name: str, *, passes: int, trials: int, lane: str) -> PassAtKResult:
        trial_runs = tuple(
            ScenarioResult(
                spec=_spec(name, lane=lane),
                run=_run(name, with_sentinel=True),
                matcher_results=(),
                skipped=False,
            )
            for _ in range(trials)
        )
        return PassAtKResult(
            spec_name=name,
            trials=trials,
            passes=passes,
            require="any",
            skipped=False,
            cost_usd=0.5,
            trial_results=trial_runs,
        )

    def test_pass_fraction_rendered_and_sentinel_absent(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "teatree.eval.report.find_spec",
            lambda name: _spec(name, lane="under_load"),
        )
        result = self._pass_at_k("gamma", passes=2, trials=3, lane="under_load")
        md = render_summary_markdown([result])
        assert SENTINEL not in md
        assert "gamma" in md
        assert "2/3" in md
        assert "under_load" in md

    def test_lane_looked_up_from_spec_when_absent_on_result(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "teatree.eval.report.find_spec",
            lambda name: _spec(name, lane="clean_room"),
        )
        result = self._pass_at_k("delta", passes=3, trials=3, lane="clean_room")
        md = render_summary_markdown([result])
        assert "clean_room" in md
        assert "pass" in md

    def test_failing_multi_trial_shows_fail_verdict(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "teatree.eval.report.find_spec",
            lambda name: _spec(name, lane="clean_room"),
        )
        result = self._pass_at_k("eps", passes=0, trials=3, lane="clean_room")
        md = render_summary_markdown([result])
        assert "fail" in md
        assert "0/3" in md

    def test_lane_falls_back_to_unknown_when_spec_not_found(self, monkeypatch) -> None:
        # A pass@k row whose name has no catalog spec renders ``unknown`` for the
        # lane rather than crashing — the publish-safe degraded path.
        monkeypatch.setattr("teatree.eval.report.find_spec", lambda _name: None)
        result = self._pass_at_k("orphan", passes=1, trials=1, lane="clean_room")
        md = render_summary_markdown([result])
        assert "unknown" in md
        assert "orphan" in md


class TestModelHeaderFallback:
    def test_empty_results_renders_unknown_model(self) -> None:
        md = render_summary_markdown([])
        assert "`unknown`" in md
        assert "0 passed" in md

    def test_pass_at_k_with_no_spec_renders_unknown_model(self, monkeypatch) -> None:
        monkeypatch.setattr("teatree.eval.report.find_spec", lambda _name: None)
        result = PassAtKResult(
            spec_name="orphan",
            trials=1,
            passes=1,
            require="any",
            skipped=False,
            cost_usd=0.0,
            trial_results=(),
        )
        assert _model_of([result]) == "unknown"
        md = render_summary_markdown([result])
        assert "`unknown`" in md


class TestLaneOf:
    def test_explicit_lane_is_returned_verbatim(self) -> None:
        # ``_lane_of`` returns a caller-supplied lane without a catalog lookup —
        # the explicit-lane branch of the helper.
        assert _lane_of("any-name", "under_load") == "under_load"

    def test_unknown_when_lane_absent_and_spec_missing(self, monkeypatch) -> None:
        monkeypatch.setattr("teatree.eval.report.find_spec", lambda _name: None)
        assert _lane_of("ghost", None) == "unknown"

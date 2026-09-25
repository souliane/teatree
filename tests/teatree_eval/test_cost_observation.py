"""What a run COST, and whether that figure was measured at all.

The control this file exists for: a run that made metered calls must never report
``$0.00``. The ``anthropic_api`` backend — the one every CI eval lane runs — drives
the model through ``PydanticAiRunner``, whose ``total_cost_usd`` is ``None`` for
Anthropic, so the report asserted ``API cost: $0.00 (no metered calls)`` on a lane
that was spending on a metered key. That line is worse than absent: it is the
reassurance that would stop someone noticing runaway cost.

The second control is the other half of the same honesty: a run whose cost is
genuinely unobservable reports *unknown*, never ``$0.00`` — an unmeasured value and
a measured zero must be distinguishable in the output.
"""

import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from teatree.core.cost import price_for_model
from teatree.eval.cost_observation import UNKNOWN_COST, observe_cost
from teatree.eval.message_mapping import eval_run_from_messages
from teatree.eval.models import (
    COST_SOURCE_DERIVED,
    COST_SOURCE_NOT_METERED,
    COST_SOURCE_REPORTED,
    COST_SOURCE_UNKNOWN,
    EvalRun,
    EvalSpec,
)
from teatree.eval.report import ScenarioResult, render_json, render_text
from teatree.eval.summary_markdown import render_summary_markdown
from teatree.eval.transcript import StreamJsonEvent

_MODEL = "claude-opus-4-8"

# A real anthropic_api turn's usage, in the four keys the Messages API bills on.
_USAGE = {
    "input_tokens": 12_000,
    "output_tokens": 3_000,
    "cache_read_input_tokens": 40_000,
    "cache_creation_input_tokens": 8_000,
}


def _spec(name: str = "s") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="text",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        model=_MODEL,
    )


def _result(
    *,
    total_cost_usd: float | None,
    usage: dict[str, int] | None,
    model_usage: dict[str, dict[str, object]] | None,
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=total_cost_usd,
        usage=usage,
        model_usage=model_usage,
        result="ok",
    )


def _anthropic_api_messages(
    *, total_cost_usd: float | None = None, usage: dict[str, int] | None = None
) -> list[object]:
    """The message shape ``AnthropicApiRunner`` actually yields.

    ``total_cost_usd`` is ``None`` (``router_reported_cost`` finds no cost key on an
    Anthropic run), ``usage`` carries the real token split, and ``model_usage`` is the
    identity-only map ``{model: {}}`` the pydantic_ai session emits.
    """
    return [
        AssistantMessage(content=[TextBlock(text="answer")], model=_MODEL),
        _result(
            total_cost_usd=total_cost_usd,
            usage=_USAGE if usage is None else usage,
            model_usage={_MODEL: {}},
        ),
    ]


def _scenario_result(run: EvalRun, *, skipped: bool = False) -> ScenarioResult:
    return ScenarioResult(spec=_spec(run.spec_name), run=run, matcher_results=(), skipped=skipped)


def _expected_usd() -> float:
    return price_for_model(_MODEL).cost(
        input_tokens=_USAGE["input_tokens"],
        output_tokens=_USAGE["output_tokens"],
        cache_read_tokens=_USAGE["cache_read_input_tokens"],
        cache_write_tokens=_USAGE["cache_creation_input_tokens"],
    )


class TestMeteredRunCannotReportZero:
    """THE control: a run that made metered calls cannot report ``$0.00``."""

    def test_anthropic_api_run_reports_a_positive_cost(self) -> None:
        run = eval_run_from_messages(_spec(), _anthropic_api_messages())
        assert run.cost_usd == pytest.approx(_expected_usd())
        assert run.cost_source == COST_SOURCE_DERIVED
        # Pinned independently of PRICE_TABLE, so a silent rate change is visible here
        # rather than agreeing with itself: opus is $5/$25 per MTok with the documented
        # 0.10x read / 1.25x write cache multipliers, so 12k in + 3k out + 40k read +
        # 8k write = 60000 + 75000 + 20000 + 50000 micro-dollars.
        assert run.cost_usd == pytest.approx(0.2050)

    def test_summary_line_never_claims_no_metered_calls_for_a_run_that_spent(self) -> None:
        results = [_scenario_result(eval_run_from_messages(_spec(), _anthropic_api_messages()))]
        text = render_text(results)
        assert "$0.00 (no metered calls)" not in text
        assert f"API cost: ${_expected_usd():.4f}" in text

    def test_figure_scales_with_usage_not_with_scenario_count(self) -> None:
        """Derived from the run's OWN tokens — doubling the usage doubles the figure."""
        doubled = {key: value * 2 for key, value in _USAGE.items()}
        single = eval_run_from_messages(_spec(), _anthropic_api_messages())
        double = eval_run_from_messages(_spec(), _anthropic_api_messages(usage=doubled))
        assert single.cost_usd > 0.0
        assert double.cost_usd == pytest.approx(single.cost_usd * 2)

    def test_provider_reported_cost_wins_over_the_derivation(self) -> None:
        run = eval_run_from_messages(_spec(), _anthropic_api_messages(total_cost_usd=0.42))
        assert run.cost_usd == pytest.approx(0.42)
        assert run.cost_source == COST_SOURCE_REPORTED


class TestUnmeasuredCostIsNotZero:
    """An unmeasured value and a measured zero must be distinguishable in the output."""

    def test_run_with_no_cost_and_no_usage_is_unknown(self) -> None:
        run = eval_run_from_messages(_spec(), _anthropic_api_messages(usage={}))
        assert run.cost_source == COST_SOURCE_UNKNOWN

    def test_summary_says_unknown_rather_than_zero(self) -> None:
        results = [_scenario_result(eval_run_from_messages(_spec(), _anthropic_api_messages(usage={})))]
        text = render_text(results)
        assert "API cost: unknown" in text
        assert "API cost: $0.00" not in text
        assert "no metered calls" not in text

    def test_replayed_transcript_reports_a_measured_zero(self) -> None:
        """A recorded-transcript replay runs no model — ``$0.00`` there is the truth."""
        run = EvalRun(
            spec_name="s",
            tool_calls=(),
            text_blocks=("hi",),
            terminal_reason="success",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
            cost_source=COST_SOURCE_NOT_METERED,
        )
        text = render_text([_scenario_result(run)])
        assert "API cost: $0.00 (no metered calls)" in text

    def test_json_summary_counts_the_unmeasurable_runs(self) -> None:
        results = [
            _scenario_result(eval_run_from_messages(_spec(), _anthropic_api_messages())),
            _scenario_result(eval_run_from_messages(_spec(), _anthropic_api_messages(usage={}))),
        ]
        summary = json.loads(render_json(results))["summary"]
        assert summary["cost_unknown_runs"] == 1
        assert summary["total_cost_usd"] == pytest.approx(_expected_usd())


class TestUnpriceableModelIsNeverGuessed:
    """A swapped non-Claude model has no built-in rate — say unknown, never guess one."""

    def test_non_claude_model_with_real_usage_reports_unknown(self) -> None:
        spec = dataclasses.replace(_spec(), model="deepseek/deepseek-chat")
        messages = [
            AssistantMessage(content=[TextBlock(text="answer")], model="deepseek/deepseek-chat"),
            _result(total_cost_usd=None, usage=_USAGE, model_usage={"deepseek/deepseek-chat": {}}),
        ]
        run = eval_run_from_messages(spec, messages)
        assert run.cost_source == COST_SOURCE_UNKNOWN
        assert "API cost: unknown" in render_text([_scenario_result(run)])

    def test_configured_price_override_makes_it_derivable(self) -> None:
        override = {"deepseek/": {"input": 0.5, "output": 1.5}}
        spec = dataclasses.replace(_spec(), model="deepseek/deepseek-chat")
        messages = [
            AssistantMessage(content=[TextBlock(text="answer")], model="deepseek/deepseek-chat"),
            _result(total_cost_usd=None, usage=_USAGE, model_usage={"deepseek/deepseek-chat": {}}),
        ]
        with patch("teatree.core.cost.cold_reader.read_setting", return_value=override):
            run = eval_run_from_messages(spec, messages)
        assert run.cost_source == COST_SOURCE_DERIVED
        assert run.cost_usd > 0.0


class TestSummaryMarkdownIsHonestToo:
    """The published dashboard carries the same distinction as the text report."""

    def test_unmeasurable_run_renders_unknown_in_the_cost_cell(self) -> None:
        result = _scenario_result(eval_run_from_messages(_spec(), _anthropic_api_messages(usage={})))
        markdown = render_summary_markdown([result])
        assert "| unknown |" in markdown
        assert "1 run(s) cost unknown" in markdown

    def test_derived_run_renders_its_figure(self) -> None:
        result = _scenario_result(eval_run_from_messages(_spec(), _anthropic_api_messages()))
        markdown = render_summary_markdown([result])
        assert f"| ${_expected_usd():.4f} |" in markdown
        assert "cost unknown" not in markdown


def _usage_only_events() -> list[StreamJsonEvent]:
    """The result event `PydanticAiRunner` yields: no transport cost, real token usage."""
    event = StreamJsonEvent.from_obj(
        1,
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "total_cost_usd": None,
            "usage": _USAGE,
            "model_usage": {_MODEL: {}},
        },
    )
    assert event is not None
    return [event]


class TestATransportThatReportsItsOwnBill:
    """Usage pricing is a FALLBACK for transports that bill silently, not a universal.

    A transport whose own `total_cost_usd` is authoritative reports `None` when it
    billed nothing. Pricing that `None` from token usage manufactures a figure the
    transport contradicts, and every $0-metered guard downstream then sees a
    non-zero total and stays green.
    """

    def test_a_transport_that_reports_its_own_bill_is_never_priced_from_usage(self) -> None:
        assert observe_cost(_usage_only_events(), requested_model=_MODEL, price_from_usage=False) == UNKNOWN_COST

    def test_the_default_still_prices_an_unreported_run_from_its_usage(self) -> None:
        observed = observe_cost(_usage_only_events(), requested_model=_MODEL)

        assert observed.source == COST_SOURCE_DERIVED
        assert observed.usd == pytest.approx(_expected_usd())

    def test_the_unknown_line_never_denies_the_usage_this_run_reported(self) -> None:
        """This run's tokens ARE observable — the line must say unpriced, not unobserved.

        "neither a transport cost nor token usage" sends a reader of a $0 api lane hunting
        a credential fault that does not exist, which is the misdiagnosis this branch of
        the report exists to prevent.
        """
        run = eval_run_from_messages(_spec(), _anthropic_api_messages(), price_from_usage=False)

        text = render_text([_scenario_result(run)])

        assert run.usage.total_input > 0
        assert "nor token usage" not in text
        assert "API cost: unknown" in text
        assert "not priced from token usage" in text
        assert "This is NOT $0.00." in text

    def test_a_reported_figure_wins_over_the_flag(self) -> None:
        """The flag suppresses the DERIVATION, never a bill the transport actually stated."""
        event = StreamJsonEvent.from_obj(
            1, {"type": "result", "subtype": "success", "total_cost_usd": 0.03, "usage": _USAGE}
        )
        assert event is not None

        observed = observe_cost([event], requested_model=_MODEL, price_from_usage=False)

        assert observed.source == COST_SOURCE_REPORTED
        assert observed.usd == pytest.approx(0.03)

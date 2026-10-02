"""Text, JSON, and HTML report rendering for one or more :class:`EvalRun` results."""

import dataclasses
import json
import re
from collections.abc import Callable
from html import escape

from teatree.eval.matcher_json import MatcherJson, matcher_json_dict
from teatree.eval.matcher_vacuity import is_positive_anchor
from teatree.eval.matchers import (
    ArgPattern,
    CallPattern,
    assert_assistant_text_contains,
    assert_assistant_text_matching,
    assert_final_state_contains,
    assert_final_state_matching,
    assert_no_tool_call_before,
    assert_no_tool_call_contains,
    assert_no_tool_call_matching,
    assert_tool_call_contains,
    assert_tool_call_matching,
    without_exempt_calls,
)
from teatree.eval.models import (
    CAP_TERMINAL_REASONS,
    COST_SOURCE_DERIVED,
    COST_SOURCE_REPORTED,
    COST_SOURCE_UNKNOWN,
    AnyOf,
    AssistantTextMatcher,
    EvalRun,
    EvalSpec,
    ExpectItem,
    FinalStateMatcher,
    Matcher,
    PlanBeforeToolMatcher,
    SuccessfulToolCallMatcher,
    canonicalize_tool,
)
from teatree.eval.successful_call_matcher import assert_successful_tool_call_before
from teatree.eval.trajectory_matchers import assert_visible_plan_before_first_tool


@dataclasses.dataclass(frozen=True)
class MatcherResult:
    matcher: ExpectItem
    passed: bool
    message: str


@dataclasses.dataclass(frozen=True)
class JudgeOutcome:
    """The LLM-judge verdict folded into a scenario result."""

    passed: bool
    skipped: bool
    rationale: str


#: An injected judge grader: maps a spec + its captured run to a verdict.
JudgeGrader = Callable[[EvalSpec, EvalRun], JudgeOutcome]


@dataclasses.dataclass(frozen=True)
class ScenarioResult:
    spec: EvalSpec
    run: EvalRun
    matcher_results: tuple[MatcherResult, ...]
    skipped: bool
    judge: JudgeOutcome | None = None

    @property
    def passed(self) -> bool:
        if self.skipped:
            return False
        if self.run.is_error:
            return False
        # A cap-truncated run never counts as a pass (#2192) unless single_action-exempt.
        if self.run.terminal_reason in CAP_TERMINAL_REASONS and not self._single_action_cap_exempt:
            return False
        # A judge-only spec has no deterministic evidence when the optional
        # --judge lane was not enabled. Matcher-backed specs remain gradable.
        if not self.matcher_results and self.spec.judge is not None and self.judge is None:
            return False
        if not all(m.passed for m in self.matcher_results):
            return False
        return self.judge is None or (not self.judge.skipped and self.judge.passed)

    @property
    def verdict(self) -> str:
        if self.skipped:
            return "skip"
        return "pass" if self.passed else "fail"

    @property
    def gate_assisted(self) -> bool:
        """A PASS that a #807-class production-hook Stop block carried over the line.

        The honesty annotation: gate-firing is NEVER a pass condition (that would
        force-FAIL a first-try-compliant model — the gate only fires on
        non-compliance), so a gate-carried pass is surfaced here (rendered
        ``pass (gate-assisted)``) rather than hidden, keeping model-alone
        regressions from masquerading as clean passes.
        """
        return self.passed and any(event.is_stop_block for event in self.run.gate_events)

    @property
    def cap_truncated_matchers_satisfied(self) -> bool:
        """A FAIL the CAP produced, not the agent: every matcher (and the judge) passed.

        ``passed`` returns ``False`` on a cap terminal reason even when the graded
        behaviour was observed (#2192), so a sandbox the agent burned its turn budget
        probing is recorded with the same ``FAIL`` as genuine non-compliance — and the
        two are indistinguishable without downloading the per-trial transcript. This
        is the annotation that separates them; it is NEVER a pass condition (the
        verdict stays ``fail``), so the gate keeps full teeth. A positive anchor is
        required, so a negative-only run that did nothing and timed out is not
        laundered into "matchers satisfied".
        """
        if self.skipped or self.passed or self.run.is_error:
            return False
        if self.run.terminal_reason not in CAP_TERMINAL_REASONS:
            return False
        if not all(m.passed for m in self.matcher_results):
            return False
        if not any(is_positive_anchor(m.matcher) for m in self.matcher_results):
            return False
        return self.judge is None or (not self.judge.skipped and self.judge.passed)

    @property
    def _single_action_cap_exempt(self) -> bool:
        return (
            self.spec.single_action
            and any(is_positive_anchor(m.matcher) for m in self.matcher_results)
            and all(m.passed for m in self.matcher_results)
        )


def evaluate(spec: EvalSpec, run: EvalRun, *, judge: "JudgeGrader | None" = None) -> ScenarioResult:
    """Apply the matchers (and, when configured, the LLM judge) to a run.

    ``judge`` is an injected grader (any callable mapping ``(spec, run)`` to a
    :class:`JudgeOutcome`). It runs only when the spec carries a ``judge`` block,
    so matcher-based scenarios are untouched and the subprocess judge is never a
    hidden dependency of the default path.
    """
    skipped = run.terminal_reason.startswith("skipped:")
    if skipped:
        return ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=True)
    # A judge-only spec (a judge block, zero matchers) has no deterministic teeth;
    # without an injected grader there is nothing to grade, so it is SKIPPED for
    # setup reasons (surfaced as needs-setup by the AI lane) rather than read as a
    # vacuous green. A spec that also carries matchers still grades them here — only
    # the pure judge-only case is ungradable without a grader.
    if not spec.matchers and spec.judge is not None and judge is None:
        ungraded = dataclasses.replace(
            run, terminal_reason="skipped: judge-only spec, no judge grader injected on this lane"
        )
        return ScenarioResult(spec=spec, run=ungraded, matcher_results=(), skipped=True)
    results: list[MatcherResult] = []
    for matcher in spec.matchers:
        try:
            _dispatch(matcher, run)
        except AssertionError as exc:
            results.append(MatcherResult(matcher=matcher, passed=False, message=str(exc)))
        else:
            results.append(MatcherResult(matcher=matcher, passed=True, message=""))
    judge_outcome = judge(spec, run) if (judge is not None and spec.judge is not None) else None
    return ScenarioResult(
        spec=spec,
        run=run,
        matcher_results=tuple(results),
        skipped=False,
        judge=judge_outcome,
    )


def _dispatch(matcher: ExpectItem, run: EvalRun) -> None:
    if isinstance(matcher, AnyOf):
        _dispatch_any_of(matcher, run)
        return
    if isinstance(matcher, SuccessfulToolCallMatcher):
        assert_successful_tool_call_before(
            run,
            CallPattern(_canonicalize_tool(matcher.tool), matcher.arg_path, _as_regex(matcher.operator, matcher.value)),
            _as_regex(matcher.result_operator, matcher.result_value),
            CallPattern(
                _canonicalize_tool(matcher.before_tool),
                matcher.before_arg_path,
                _as_regex(matcher.before_operator, matcher.before_value),
            ),
        )
        return
    if isinstance(matcher, FinalStateMatcher):
        _dispatch_final_state(matcher, run)
        return
    if isinstance(matcher, AssistantTextMatcher | PlanBeforeToolMatcher):
        if isinstance(matcher, AssistantTextMatcher):
            _dispatch_assistant_text(matcher, run)
        else:
            assert_visible_plan_before_first_tool(
                run,
                governed_tools=matcher.governed_tools,
                patterns=matcher.patterns,
            )
        return
    tool = _canonicalize_tool(matcher.tool)
    if matcher.kind == "positive" and matcher.operator in {"contains", "~"}:
        if matcher.operator == "contains":
            assert_tool_call_contains(run, tool, matcher.arg_path, matcher.value)
        else:
            assert_tool_call_matching(run, tool, matcher.arg_path, matcher.value)
        return
    if matcher.kind == "negative":
        _dispatch_negative(matcher, run, tool)
        return
    msg = f"unsupported matcher operator: kind={matcher.kind!r}, operator={matcher.operator!r}"
    raise NotImplementedError(msg)


def _dispatch_negative(matcher: Matcher, run: EvalRun, tool: str) -> None:
    if matcher.has_exemption:
        exempt = ArgPattern(matcher.unless_arg_path, _as_regex(matcher.unless_operator, matcher.unless_value))
        run = without_exempt_calls(run, exempt)
    if matcher.has_order_guard:
        forbidden = CallPattern(tool, matcher.arg_path, _as_regex(matcher.operator, matcher.value))
        guard = CallPattern(
            _canonicalize_tool(matcher.guard_tool),
            matcher.guard_arg_path,
            _as_regex(matcher.guard_operator, matcher.guard_value),
        )
        assert_no_tool_call_before(run, forbidden, guard)
        return
    if matcher.operator == "~":
        assert_no_tool_call_matching(run, tool, matcher.arg_path, matcher.value)
        return
    if matcher.operator == "contains":
        assert_no_tool_call_contains(run, tool, matcher.arg_path, matcher.value)
        return
    msg = f"unsupported matcher operator: kind={matcher.kind!r}, operator={matcher.operator!r}"
    raise NotImplementedError(msg)


def _dispatch_any_of(matcher: AnyOf, run: EvalRun) -> None:
    """Pass when ANY alternative holds; else raise with every branch's failure."""
    branch_messages: list[str] = []
    for alternative in matcher.alternatives:
        try:
            _dispatch(alternative, run)
        except AssertionError as exc:
            branch_messages.append(str(exc))
        else:
            return
    joined = "\n  --- or ---\n".join(branch_messages)
    msg = f"Expected ANY of {len(matcher.alternatives)} alternatives to hold; all failed:\n{joined}"
    raise AssertionError(msg)


def _dispatch_final_state(matcher: FinalStateMatcher, run: EvalRun) -> None:
    if matcher.operator == "contains":
        assert_final_state_contains(run, matcher.value)
        return
    if matcher.operator == "~":
        assert_final_state_matching(run, matcher.value)
        return
    msg = f"unsupported final_state operator: {matcher.operator!r}"
    raise NotImplementedError(msg)


def _dispatch_assistant_text(matcher: AssistantTextMatcher, run: EvalRun) -> None:
    if matcher.operator == "contains":
        assert_assistant_text_contains(run, matcher.value)
        return
    if matcher.operator == "~":
        assert_assistant_text_matching(run, matcher.value)
        return
    msg = f"unsupported assistant_text operator: {matcher.operator!r}"
    raise NotImplementedError(msg)


def _canonicalize_tool(name: str) -> str:
    return canonicalize_tool(name)


def _as_regex(operator: str, value: str) -> str:
    """A regex form of an ``op "value"`` matcher — a ``contains`` value is escaped."""
    return value if operator == "~" else re.escape(value)


#: The one sentence a maintainer needs to route a cap-truncated red to the HARNESS
#: (turn budget, a missing ``cli_stubs``/``fixture`` the prompt presupposes) instead
#: of re-reading the skill for a compliance bug that is not there.
_CAP_TRUNCATED_NOTE = (
    "cap-truncated: every matcher passed; the run then hit '{reason}'. "
    "Diagnose the harness (turn budget, missing cli_stubs/fixture), not the agent's compliance."
)


def render_text(results: list[ScenarioResult]) -> str:
    lines: list[str] = []
    for result in results:
        if result.skipped:
            lines.append(f"SKIP {result.spec.name}: {result.run.terminal_reason}")
            continue
        status = "PASS" if result.passed else "FAIL"
        judge_tag = " [judge]" if result.judge is not None and not result.judge.skipped else ""
        gate_tag = " (gate-assisted)" if result.gate_assisted else ""
        cap_tag = " (cap-truncated)" if result.cap_truncated_matchers_satisfied else ""
        lines.append(f"{status}{gate_tag}{cap_tag} {result.spec.name} ({result.run.terminal_reason}){judge_tag}")
        if result.cap_truncated_matchers_satisfied:
            lines.append(f"  - {_CAP_TRUNCATED_NOTE.format(reason=result.run.terminal_reason)}")
        if not result.passed:
            for matcher_result in result.matcher_results:
                if matcher_result.passed:
                    continue
                lines.append("  -")
                lines.extend(f"    {body_line}" for body_line in matcher_result.message.splitlines())
            if result.judge is not None and not result.judge.skipped and not result.judge.passed:
                lines.append(f"  - judge: {result.judge.rationale}")
            # Reported even when a matcher also failed: on an errored run the matcher
            # failed BECAUSE the trajectory is empty, so the cause is the only fact worth reading.
            if result.run.is_error:
                lines.append(f"  - run errored: {result.run.terminal_reason}")
                if result.run.raw_stderr.strip():
                    lines.append(f"    stderr: {result.run.raw_stderr.strip()[:500]}")
    summary = _summary(results)
    cost = _cost_summary(results)
    lines.extend(("", summary, cost))
    return "\n".join(lines)


def render_json(results: list[ScenarioResult]) -> str:
    payload = {
        "scenarios": [
            {
                "name": r.spec.name,
                "terminal_reason": r.run.terminal_reason,
                "is_error": r.run.is_error,
                "skipped": r.skipped,
                "passed": r.passed,
                "gate_assisted": r.gate_assisted,
                "cap_truncated_matchers_satisfied": r.cap_truncated_matchers_satisfied,
                "gate_events": [
                    {
                        "hook_event": e.hook_event_name,
                        "outcome": e.outcome,
                        "is_stop_block": e.is_stop_block,
                        "sequence": e.sequence,
                        "tool_name": e.tool_name,
                        "tool_use_id": e.tool_use_id,
                        "gate_id": e.gate_id,
                        "reason": e.reason,
                        "assistant_text": e.assistant_text,
                    }
                    for e in r.run.gate_events
                ],
                "judge": (
                    None
                    if r.judge is None
                    else {"passed": r.judge.passed, "skipped": r.judge.skipped, "rationale": r.judge.rationale}
                ),
                "tool_calls": [{"name": c.name, "input": c.input, "turn": c.turn} for c in r.run.tool_calls],
                "text_blocks": list(r.run.text_blocks),
                "matchers": [
                    matcher_json_dict(MatcherJson.of_result(m.matcher, passed=m.passed, message=m.message))
                    for m in r.matcher_results
                ],
            }
            for r in results
        ],
        "summary": _summary_dict(results),
    }
    return json.dumps(payload, indent=2)


_HTML_STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 system-ui, sans-serif; margin: 2rem; max-width: 60rem; }
h1 { font-size: 1.4rem; }
.summary { margin: 0 0 1.5rem; font-weight: 600; }
.summary .pass { color: #1a7f37; }
.summary .fail { color: #cf222e; }
.summary .skip { color: #6e7781; }
details { border: 1px solid #d0d7de; border-radius: 6px; margin: 0.5rem 0; padding: 0.5rem 0.75rem; }
details.pass { border-left: 4px solid #1a7f37; }
details.fail { border-left: 4px solid #cf222e; }
details.skip { border-left: 4px solid #6e7781; }
summary { cursor: pointer; font-weight: 600; }
.verdict { font-size: 0.8rem; padding: 0.1rem 0.45rem; border-radius: 999px; margin-right: 0.5rem; color: #fff; }
.verdict.pass { background: #1a7f37; }
.verdict.fail { background: #cf222e; }
.verdict.skip { background: #6e7781; }
.reason { color: #6e7781; font-weight: 400; }
ul.matchers { margin: 0.5rem 0 0; }
pre { white-space: pre-wrap; background: rgba(127,127,127,0.1); padding: 0.5rem; border-radius: 4px; }
.judge { margin-top: 0.5rem; }
""".strip()


def render_html(results: list[ScenarioResult]) -> str:
    """Render a self-contained HTML report (inline CSS, no external assets).

    Sibling of :func:`render_text` / :func:`render_json` — same
    ``list[ScenarioResult]`` input contract. Every piece of run-derived content
    (scenario name, terminal reason, matcher message, judge rationale) is
    HTML-escaped so a transcript value can never inject markup.
    """
    counts = _summary_dict(results)
    summary = (
        f'<p class="summary">'
        f'<span class="pass">{counts["passed"]} passed</span>, '
        f'<span class="fail">{counts["failed"]} failed</span>, '
        f'<span class="skip">{counts["skipped"]} skipped</span> '
        f"(of {counts['total']})</p>"
    )
    rows = "\n".join(_html_scenario(result) for result in results)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Eval report</title>\n"
        f"<style>{_HTML_STYLE}</style>\n"
        "</head>\n<body>\n<h1>Eval report</h1>\n"
        f"{summary}\n{rows}\n</body>\n</html>\n"
    )


def _html_scenario(result: ScenarioResult) -> str:
    verdict = result.verdict
    name = escape(result.spec.name)
    reason = escape(result.run.terminal_reason)
    head = (
        f'<summary><span class="verdict {verdict}">{verdict.upper()}</span>'
        f'{name} <span class="reason">({reason})</span></summary>'
    )
    body_parts: list[str] = []
    if result.cap_truncated_matchers_satisfied:
        note = escape(_CAP_TRUNCATED_NOTE.format(reason=result.run.terminal_reason))
        body_parts.append(f'<p class="judge"><strong>{note}</strong></p>')
    failed_matchers = [m for m in result.matcher_results if not m.passed]
    if failed_matchers:
        items = "\n".join(f"<li><pre>{escape(m.message)}</pre></li>" for m in failed_matchers)
        body_parts.append(f'<ul class="matchers">\n{items}\n</ul>')
    if result.judge is not None and not result.judge.skipped:
        judge_verdict = "pass" if result.judge.passed else "fail"
        body_parts.append(
            f'<p class="judge"><strong>judge ({judge_verdict}):</strong> {escape(result.judge.rationale)}</p>'
        )
    if result.run.is_error:
        body_parts.append(f'<p class="judge"><strong>run errored:</strong> {reason}</p>')
        if result.run.raw_stderr.strip():
            body_parts.append(f"<pre>{escape(result.run.raw_stderr.strip()[:500])}</pre>")
    body = "\n".join(body_parts)
    return f'<details class="{verdict}">\n{head}\n{body}\n</details>'


def _summary(results: list[ScenarioResult]) -> str:
    counts = _summary_dict(results)
    return (
        f"summary: {counts['passed']} passed, {counts['failed']} failed, "
        f"{counts['skipped']} skipped (of {counts['total']})"
    )


def cost_cell(run: EvalRun) -> str:
    """One run's cost as a table cell — the figure, or ``unknown`` when nothing measured it."""
    return "unknown" if run.cost_source == COST_SOURCE_UNKNOWN else f"${run.cost_usd:.4f}"


def _cost_summary(results: list[ScenarioResult]) -> str:
    """The run's spend, or an explicit *unknown* — never a ``$0.00`` nobody measured.

    ``$0.00 (no metered calls)`` is only ever emitted for runs whose zero was MEASURED
    (a recorded-transcript replay, a skip, a transport that reported a real zero). A run
    that executed and whose cost was never established — no transport figure, and none
    derived from usage — renders *unknown*, because the two must be distinguishable to a
    reader watching for spend.
    """
    counts = _summary_dict(results)
    priced, unknown = int(counts["priced_runs"]), int(counts["cost_unknown_runs"])
    if priced:
        line = f"API cost: ${counts['total_cost_usd']:.4f} over {priced} priced run(s){_cost_basis(results)}"
        return line if not unknown else f"{line}; {unknown} further run(s) unmeasurable (cost unknown)"
    if unknown:
        return (
            f"API cost: unknown — {unknown} executed run(s) report no transport cost and were "
            "not priced from token usage, so nothing was measured. This is NOT $0.00."
        )
    return "API cost: $0.00 (no metered calls)"


def _cost_basis(results: list[ScenarioResult]) -> str:
    """How the priced runs were priced, so a list-price derivation never reads as a bill."""
    sources = {r.run.cost_source for r in results if not r.skipped and r.run.cost_usd > 0}
    if sources == {COST_SOURCE_DERIVED}:
        return " (derived from token usage at list price)"
    return "" if sources == {COST_SOURCE_REPORTED} else " (transport-reported and usage-derived)"


def _summary_dict(results: list[ScenarioResult]) -> dict[str, int | float]:
    total = len(results)
    skipped = sum(1 for r in results if r.skipped)
    passed = sum(1 for r in results if r.passed and not r.skipped)
    failed = total - passed - skipped
    total_cost_usd = sum(r.run.cost_usd for r in results)
    priced_runs = sum(1 for r in results if r.run.cost_usd > 0)
    cost_unknown_runs = sum(1 for r in results if not r.skipped and r.run.cost_source == COST_SOURCE_UNKNOWN)
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total_cost_usd": total_cost_usd,
        "priced_runs": priced_runs,
        # Executed runs whose cost could not be established at all. A consumer summing
        # `total_cost_usd` without reading this is summing over an unknown denominator.
        "cost_unknown_runs": cost_unknown_runs,
    }

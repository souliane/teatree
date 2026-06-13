"""Text, JSON, and HTML report rendering for one or more :class:`EvalRun` results."""

import dataclasses
import json
from collections.abc import Callable
from html import escape

from teatree.eval.matchers import (
    assert_final_state_contains,
    assert_final_state_matching,
    assert_no_tool_call_matching,
    assert_tool_call_contains,
    assert_tool_call_matching,
)
from teatree.eval.models import (
    CAP_TERMINAL_REASONS,
    AnyOf,
    EvalRun,
    EvalSpec,
    ExpectItem,
    FinalStateMatcher,
    Matcher,
    canonicalize_tool,
)


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
            return True
        if self.run.is_error:
            return False
        # A cap-truncated run (max_turns/budget/watchdog) NEVER counts as a gate
        # pass, even when its partial trajectory satisfied every matcher (#2192).
        # ``_terminal_capped_run`` grades the partial trajectory with
        # ``is_error=False`` so the reason stays visible (diagnostic), but a run
        # that emitted the expected early behavior yet never finished must FAIL
        # the gate — otherwise raising the caps (#19) masks real failures.
        if self.run.terminal_reason in CAP_TERMINAL_REASONS:
            return False
        if not all(m.passed for m in self.matcher_results):
            return False
        return self.judge is None or self.judge.skipped or self.judge.passed

    @property
    def verdict(self) -> str:
        if self.skipped:
            return "skip"
        return "pass" if self.passed else "fail"


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
    if isinstance(matcher, FinalStateMatcher):
        _dispatch_final_state(matcher, run)
        return
    tool = _canonicalize_tool(matcher.tool)
    if matcher.kind == "positive" and matcher.operator == "contains":
        assert_tool_call_contains(run, tool, matcher.arg_path, matcher.value)
        return
    if matcher.kind == "positive" and matcher.operator == "~":
        assert_tool_call_matching(run, tool, matcher.arg_path, matcher.value)
        return
    if matcher.kind == "negative" and matcher.operator == "~":
        assert_no_tool_call_matching(run, tool, matcher.arg_path, matcher.value)
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


def _canonicalize_tool(name: str) -> str:
    return canonicalize_tool(name)


def render_text(results: list[ScenarioResult]) -> str:
    lines: list[str] = []
    for result in results:
        if result.skipped:
            lines.append(f"SKIP {result.spec.name}: {result.run.terminal_reason}")
            continue
        status = "PASS" if result.passed else "FAIL"
        judge_tag = " [judge]" if result.judge is not None and not result.judge.skipped else ""
        lines.append(f"{status} {result.spec.name} ({result.run.terminal_reason}){judge_tag}")
        if not result.passed:
            for matcher_result in result.matcher_results:
                if matcher_result.passed:
                    continue
                lines.append("  -")
                lines.extend(f"    {body_line}" for body_line in matcher_result.message.splitlines())
            if result.judge is not None and not result.judge.skipped and not result.judge.passed:
                lines.append(f"  - judge: {result.judge.rationale}")
            if result.run.is_error and not any(not m.passed for m in result.matcher_results):
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
                "judge": (
                    None
                    if r.judge is None
                    else {"passed": r.judge.passed, "skipped": r.judge.skipped, "rationale": r.judge.rationale}
                ),
                "tool_calls": [{"name": c.name, "input": c.input, "turn": c.turn} for c in r.run.tool_calls],
                "matchers": [_matcher_json_dict(_MatcherJson.of_result(m)) for m in r.matcher_results],
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
    failed_matchers = [m for m in result.matcher_results if not m.passed]
    if failed_matchers:
        items = "\n".join(f"<li><pre>{escape(m.message)}</pre></li>" for m in failed_matchers)
        body_parts.append(f'<ul class="matchers">\n{items}\n</ul>')
    if result.judge is not None and not result.judge.skipped:
        judge_verdict = "pass" if result.judge.passed else "fail"
        body_parts.append(
            f'<p class="judge"><strong>judge ({judge_verdict}):</strong> {escape(result.judge.rationale)}</p>'
        )
    if result.run.is_error and not failed_matchers:
        body_parts.append(f'<p class="judge"><strong>run errored:</strong> {reason}</p>')
        if result.run.raw_stderr.strip():
            body_parts.append(f"<pre>{escape(result.run.raw_stderr.strip()[:500])}</pre>")
    body = "\n".join(body_parts)
    return f'<details class="{verdict}">\n{head}\n{body}\n</details>'


@dataclasses.dataclass(frozen=True)
class _MatcherJson:
    """One matcher serialized for the JSON report.

    A single matcher fills ``tool``/``arg_path``/``operator``/``value``; an
    ``any_of`` disjunction leaves them ``None`` and lists its positive
    branches under ``alternatives`` instead.
    """

    kind: str
    passed: bool
    message: str
    tool: str | None = None
    arg_path: str | None = None
    operator: str | None = None
    value: str | None = None
    alternatives: tuple["_MatcherJson", ...] = ()

    @classmethod
    def of_matcher(cls, matcher: Matcher, *, passed: bool = True, message: str = "") -> "_MatcherJson":
        return cls(
            kind=matcher.kind,
            tool=matcher.tool,
            arg_path=matcher.arg_path,
            operator=matcher.operator,
            value=matcher.value,
            passed=passed,
            message=message,
        )

    @classmethod
    def of_result(cls, result: MatcherResult) -> "_MatcherJson":
        matcher = result.matcher
        if isinstance(matcher, AnyOf):
            return cls(
                kind="any_of",
                passed=result.passed,
                message=result.message,
                alternatives=tuple(cls.of_matcher(alt) for alt in matcher.alternatives),
            )
        if isinstance(matcher, FinalStateMatcher):
            return cls(
                kind="final_state",
                operator=matcher.operator,
                value=matcher.value,
                passed=result.passed,
                message=result.message,
            )
        return cls.of_matcher(matcher, passed=result.passed, message=result.message)


def _matcher_json_dict(matcher: _MatcherJson) -> dict[str, str | bool | list[object]]:
    """Serialize a :class:`_MatcherJson`, omitting unset (``None``) scalar keys.

    A single matcher emits its ``tool``/``arg_path``/``operator``/``value``;
    an ``any_of`` omits those and emits ``alternatives`` instead.
    """
    out: dict[str, str | bool | list[object]] = {"kind": matcher.kind}
    for key in ("tool", "arg_path", "operator", "value"):
        scalar = getattr(matcher, key)
        if scalar is not None:
            out[key] = scalar
    if matcher.alternatives:
        out["alternatives"] = [_matcher_json_dict(alt) for alt in matcher.alternatives]
    out["passed"] = matcher.passed
    out["message"] = matcher.message
    return out


def _summary(results: list[ScenarioResult]) -> str:
    counts = _summary_dict(results)
    return (
        f"summary: {counts['passed']} passed, {counts['failed']} failed, "
        f"{counts['skipped']} skipped (of {counts['total']})"
    )


def _cost_summary(results: list[ScenarioResult]) -> str:
    total_usd = sum(r.run.cost_usd for r in results)
    metered_calls = sum(1 for r in results if r.run.cost_usd > 0)
    if metered_calls == 0:
        return "API cost: $0.00 (no metered calls)"
    return f"API cost: ${total_usd:.4f} over {metered_calls} metered call(s)"


def _summary_dict(results: list[ScenarioResult]) -> dict[str, int | float]:
    total = len(results)
    skipped = sum(1 for r in results if r.skipped)
    passed = sum(1 for r in results if r.passed and not r.skipped)
    failed = total - passed - skipped
    total_cost_usd = sum(r.run.cost_usd for r in results)
    metered_calls = sum(1 for r in results if r.run.cost_usd > 0)
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total_cost_usd": total_cost_usd,
        "metered_calls": metered_calls,
    }

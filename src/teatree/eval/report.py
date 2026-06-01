"""Text and JSON report rendering for one or more :class:`EvalRun` results."""

import dataclasses
import json

from teatree.eval.matchers import assert_no_tool_call_matching, assert_tool_call_contains, assert_tool_call_matching
from teatree.eval.models import EvalRun, EvalSpec, Matcher


@dataclasses.dataclass(frozen=True)
class MatcherResult:
    matcher: Matcher
    passed: bool
    message: str


@dataclasses.dataclass(frozen=True)
class ScenarioResult:
    spec: EvalSpec
    run: EvalRun
    matcher_results: tuple[MatcherResult, ...]
    skipped: bool

    @property
    def passed(self) -> bool:
        if self.skipped:
            return True
        if self.run.is_error:
            return False
        return all(m.passed for m in self.matcher_results)

    @property
    def verdict(self) -> str:
        if self.skipped:
            return "skip"
        return "pass" if self.passed else "fail"


def evaluate(spec: EvalSpec, run: EvalRun) -> ScenarioResult:
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
    return ScenarioResult(spec=spec, run=run, matcher_results=tuple(results), skipped=False)


def _dispatch(matcher: Matcher, run: EvalRun) -> None:
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


def _canonicalize_tool(name: str) -> str:
    aliases = {"bash": "Bash"}
    return aliases.get(name.lower(), name)


def render_text(results: list[ScenarioResult]) -> str:
    lines: list[str] = []
    for result in results:
        if result.skipped:
            lines.append(f"SKIP {result.spec.name}: {result.run.terminal_reason}")
            continue
        status = "PASS" if result.passed else "FAIL"
        lines.append(f"{status} {result.spec.name} ({result.run.terminal_reason})")
        if not result.passed:
            for matcher_result in result.matcher_results:
                if matcher_result.passed:
                    continue
                lines.append("  -")
                lines.extend(f"    {body_line}" for body_line in matcher_result.message.splitlines())
            if result.run.is_error and not any(not m.passed for m in result.matcher_results):
                lines.append(f"  - run errored: {result.run.terminal_reason}")
                if result.run.raw_stderr.strip():
                    lines.append(f"    stderr: {result.run.raw_stderr.strip()[:500]}")
    summary = _summary(results)
    lines.extend(("", summary))
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
                "tool_calls": [{"name": c.name, "input": c.input, "turn": c.turn} for c in r.run.tool_calls],
                "matchers": [
                    {
                        "kind": m.matcher.kind,
                        "tool": m.matcher.tool,
                        "arg_path": m.matcher.arg_path,
                        "operator": m.matcher.operator,
                        "value": m.matcher.value,
                        "passed": m.passed,
                        "message": m.message,
                    }
                    for m in r.matcher_results
                ],
            }
            for r in results
        ],
        "summary": _summary_dict(results),
    }
    return json.dumps(payload, indent=2)


def _summary(results: list[ScenarioResult]) -> str:
    counts = _summary_dict(results)
    return (
        f"summary: {counts['passed']} passed, {counts['failed']} failed, "
        f"{counts['skipped']} skipped (of {counts['total']})"
    )


def _summary_dict(results: list[ScenarioResult]) -> dict[str, int]:
    total = len(results)
    skipped = sum(1 for r in results if r.skipped)
    passed = sum(1 for r in results if r.passed and not r.skipped)
    failed = total - passed - skipped
    return {"total": total, "passed": passed, "failed": failed, "skipped": skipped}

"""LLM-judge grading backend for non-matcher-gradeable scenarios (#1160).

Matcher-based grading (``matchers.py``) is the default and stays so: it is
deterministic, free, and the right tool when a rule reduces to "a tool call
with arg X containing Y exists". Some behaviours don't: "the explanation is
faithful to the diff", "the tone stays non-blaming", "the answer actually
addresses the question". For those a scenario opts in to an LLM judge by adding
a ``judge:`` block with a ``rubric``; this module feeds the captured transcript
plus the rubric to a judge model and parses a PASS/FAIL verdict.

Cost controls, by construction:

*   the judge model defaults to the same Sonnet tier as a run
    (``claude-sonnet-4-6``) and is per-scenario overridable to a cheaper tier;
*   ``--max-budget-usd`` caps spend per judge call and ``--max-output-tokens``
    caps the reply (a judge needs one line, not an essay);
*   a process-wide :class:`JudgeBudget` caps the number of judge calls per run,
    so a large suite cannot silently fan out into an unbounded bill.

Like the runner, the judge call runs in a virgin environment via
:func:`~teatree.eval.isolation.isolated_claude_env` plus the explicit
``--settings`` / ``--strict-mcp-config`` flags so the developer's personal
context never reaches the grader. It deliberately omits ``--bare`` for the same
reason the runner does — ``--bare`` disables ``CLAUDE_CODE_OAUTH_TOKEN`` auth
(the judge's only auth). When ``claude`` is not on PATH the judge skips
(mirrors the runner's skip path) so CI and judge-less contributors are never
blocked.
"""

import dataclasses
import re
import shutil

from teatree.eval.isolation import isolated_claude_env
from teatree.eval.models import EvalRun, EvalSpec
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

WATCHDOG_SECONDS = 120
MAX_BUDGET_USD = "0.05"
_VERDICT_RE = re.compile(r"\b(PASS|FAIL)\b", re.IGNORECASE)


class JudgeBudgetExceededError(RuntimeError):
    """Raised when a run asks for more judge calls than the configured cap."""


@dataclasses.dataclass
class JudgeBudget:
    """Process-wide cap on the number of judge model calls per run."""

    max_calls: int
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.max_calls:
            msg = f"judge budget exhausted ({self.max_calls} calls)"
            raise JudgeBudgetExceededError(msg)
        self.used += 1


@dataclasses.dataclass(frozen=True)
class JudgeVerdict:
    passed: bool
    skipped: bool
    rationale: str


def build_judge_prompt(spec: EvalSpec, run: EvalRun) -> str:
    """Render the judge prompt: rubric + a privacy-safe transcript summary.

    The summary is the agent's text blocks plus tool names and argument keys —
    enough for the judge to grade the behaviour without echoing full tool inputs
    back into a second model call.
    """
    if spec.judge is None:
        msg = "build_judge_prompt requires a spec with a judge block"
        raise ValueError(msg)
    text = "\n".join(b.strip() for b in run.text_blocks if b.strip()) or "(no text output)"
    tool_lines = [f"- {c.name}({', '.join(sorted(c.input))})" for c in run.tool_calls] or ["(no tool calls)"]
    return (
        "You are grading an AI agent's behaviour against a rubric. "
        "Reply with a single line: `PASS` or `FAIL`, then a one-sentence reason.\n\n"
        f"## Scenario\n{spec.scenario}\n\n"
        f"## Rubric (the agent passes only if ALL hold)\n{spec.judge.rubric.strip()}\n\n"
        f"## Agent text output\n{text}\n\n"
        f"## Agent tool calls\n" + "\n".join(tool_lines) + "\n"
    )


def parse_judge_verdict(stdout: str) -> tuple[bool, str]:
    """Parse the judge reply into ``(passed, rationale)``.

    The first standalone ``PASS``/``FAIL`` token decides the verdict; anything
    that never says either is treated as a FAIL (a judge that cannot commit to a
    verdict must not pass a scenario by default).
    """
    text = stdout.strip()
    match = _VERDICT_RE.search(text)
    if match is None:
        return False, "judge returned no PASS/FAIL verdict"
    return match.group(1).upper() == "PASS", text


class ClaudeJudge:
    """Grade a scenario's captured run against its rubric via ``claude -p``."""

    def __init__(self, *, budget: JudgeBudget | None = None) -> None:
        self._budget = budget

    def grade(self, spec: EvalSpec, run: EvalRun) -> JudgeVerdict:
        if spec.judge is None:
            return JudgeVerdict(passed=True, skipped=True, rationale="no judge configured")
        if run.terminal_reason.startswith("skipped:"):
            return JudgeVerdict(passed=True, skipped=True, rationale="run skipped")
        binary = shutil.which("claude")
        if binary is None:
            return JudgeVerdict(passed=True, skipped=True, rationale="claude binary not on PATH")
        if self._budget is not None:
            self._budget.consume()
        prompt = build_judge_prompt(spec, run)
        command = self._build_command(binary, spec.judge.model, prompt)
        try:
            with isolated_claude_env() as (env, cwd):
                result = run_allowed_to_fail(
                    command,
                    expected_codes=None,
                    timeout=WATCHDOG_SECONDS,
                    env=env,
                    cwd=cwd,
                )
        except TimeoutExpired:
            return JudgeVerdict(passed=False, skipped=False, rationale="judge timed out")
        passed, rationale = parse_judge_verdict(result.stdout or "")
        return JudgeVerdict(passed=passed, skipped=False, rationale=rationale)

    @staticmethod
    def _build_command(binary: str, judge_model: str, prompt: str) -> list[str]:
        return [
            binary,
            "-p",
            "--output-format",
            "text",
            "--max-turns",
            "1",
            "--max-budget-usd",
            MAX_BUDGET_USD,
            "--model",
            judge_model,
            "--no-session-persistence",
            "--disable-slash-commands",
            "--permission-mode",
            "bypassPermissions",
            "--strict-mcp-config",
            "--tools",
            "",
            "--settings",
            '{"hooks":{}}',
            prompt,
        ]

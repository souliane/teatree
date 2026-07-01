"""LLM-judge grading backend for non-matcher-gradeable scenarios (#1160).

Matcher-based grading (``matchers.py``) is the default and stays so: it is
deterministic, free, and the right tool when a rule reduces to "a tool call
with arg X containing Y exists". Some behaviours don't: "the explanation is
faithful to the diff", "the tone stays non-blaming", "the answer actually
addresses the question". For those a scenario opts in to an LLM judge by adding
a ``judge:`` block with a ``rubric``; this module feeds the captured transcript
plus the rubric to a judge model and reads a structured ``{verdict, reason}``
back off the ``ResultMessage.structured_output`` — no free-text regex.

Cost controls, by construction:

*   the judge model defaults to the same Sonnet tier as a run
    (``claude-sonnet-4-6``) and is per-scenario overridable to a cheaper tier;
*   ``max_budget_usd`` caps spend per judge call;
*   a process-wide :class:`JudgeBudget` caps the number of judge calls per run,
    so a large suite cannot silently fan out into an unbounded bill.

Like the runner, the judge runs in a virgin configuration via the shared
:func:`~teatree.eval.api_runner.build_sdk_options` clean-room builder
(``setting_sources=[]`` + a plain-string ``system_prompt`` + empty ``settings``)
so the developer's personal context never reaches the grader. When ``claude`` is
not on PATH the judge skips (mirrors the runner's skip path) so CI and
judge-less contributors are never blocked.
"""

import asyncio
import dataclasses
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from teatree.eval.api_runner import (
    CleanRoomConfig,
    build_sdk_options,
    classify_terminal_error,
    env_float,
    is_success_result_error,
)
from teatree.eval.isolation import isolated_claude_env
from teatree.eval.models import EvalRun, EvalSpec

if TYPE_CHECKING:
    from collections.abc import Mapping

WATCHDOG_SECONDS = 120

_JUDGE_BUDGET_ENV_VAR = "T3_EVAL_JUDGE_MAX_BUDGET_USD"
#: Per-judge-call cap at the per-scenario breaker's $1.00 tier; a stingy ceiling
#: truncates one grading call into a suite-aborting error. Env var overrides.
JUDGE_DEFAULT_BUDGET_USD = 1.00


def resolve_judge_budget_usd() -> float:
    """Per-judge-call cap; ``T3_EVAL_JUDGE_MAX_BUDGET_USD`` overrides (non-positive → default)."""
    return env_float(_JUDGE_BUDGET_ENV_VAR, default=JUDGE_DEFAULT_BUDGET_USD)


_JUDGE_SYSTEM_PROMPT = (
    "You are grading an AI agent's behaviour against a rubric. Decide PASS or FAIL "
    "and give a one-sentence reason. Reply via the required structured output only."
)

_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


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


@dataclasses.dataclass(frozen=True)
class StructuredVerdict:
    """The judge model's structured reply, parsed off ``ResultMessage.structured_output``."""

    verdict: str | None
    reason: str | None

    @classmethod
    def from_structured_output(cls, structured_output: object) -> "StructuredVerdict | None":
        if not isinstance(structured_output, dict):
            return None
        fields = cast("Mapping[str, object]", structured_output)
        verdict = fields.get("verdict")
        reason = fields.get("reason")
        return cls(
            verdict=verdict if isinstance(verdict, str) else None,
            reason=reason if isinstance(reason, str) else None,
        )


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
        "Grade the agent against the rubric and return the structured verdict.\n\n"
        f"## Scenario\n{spec.scenario}\n\n"
        f"## Rubric (the agent passes only if ALL hold)\n{spec.judge.rubric.strip()}\n\n"
        f"## Agent text output\n{text}\n\n"
        f"## Agent tool calls\n" + "\n".join(tool_lines) + "\n"
    )


class ClaudeJudge:
    """Grade a scenario's captured run against its rubric via the Agent SDK."""

    def __init__(self, *, budget: JudgeBudget | None = None) -> None:
        self._budget = budget

    def grade(self, spec: EvalSpec, run: EvalRun) -> JudgeVerdict:
        if spec.judge is None:
            return JudgeVerdict(passed=True, skipped=True, rationale="no judge configured")
        if run.terminal_reason.startswith("skipped:"):
            return JudgeVerdict(passed=True, skipped=True, rationale="run skipped")
        if shutil.which("claude") is None:
            return JudgeVerdict(passed=True, skipped=True, rationale="claude binary not on PATH")
        # The judge is itself a billed Claude call, so route it through the SAME
        # credential chokepoint as make_runner: export the metered ANTHROPIC_API_KEY
        # (env wins, else the pass store) and FAIL LOUD with CredentialError when no
        # key is resolvable. This runs only past the skip guards above — a
        # transcript-grade-only / keyless SKIP path (no judge block, skipped run, no
        # claude binary) grades no model, so it never reaches here and is never
        # forced to require a key. isolated_claude_env then strips the conflicting
        # OAuth token from the judge child's env using the same credential's spec,
        # so the judge authenticates on the metered API exclusively — never the
        # subscription. Imported at call time (not module top) to keep the eval CLI
        # import chain Django-free — ``credential_config`` pulls in the routing
        # models, which cannot be created before ``django.setup()``.
        from teatree.credential_config import resolve_api_key_credential  # noqa: PLC0415

        resolve_api_key_credential().export()
        if self._budget is not None:
            self._budget.consume()
        prompt = build_judge_prompt(spec, run)
        try:
            structured = asyncio.run(_drive_judge(prompt, spec.judge.model))
        except TimeoutError:
            return JudgeVerdict(passed=False, skipped=False, rationale="judge timed out")
        except Exception as exc:
            reason = classify_terminal_error(str(exc))
            if reason is None:
                raise
            return JudgeVerdict(passed=False, skipped=False, rationale=f"judge hit {reason} cap")
        return _verdict_from_structured(structured)


async def _drive_judge(prompt: str, judge_model: str) -> StructuredVerdict | None:
    with isolated_claude_env() as (env, cwd):
        options = _judge_options(model=judge_model, cwd=cwd, env=env)
        return await asyncio.wait_for(_judge_result(prompt, options), timeout=WATCHDOG_SECONDS)


def _judge_options(*, model: str, cwd: str, env: dict[str, str]) -> ClaudeAgentOptions:
    options = build_sdk_options(
        CleanRoomConfig(
            system_prompt=_JUDGE_SYSTEM_PROMPT,
            workspace=Path(cwd),
            cwd=cwd,
            env=env,
            allowed_tools=(),
            model=model,
            max_turns=1,
        )
    )
    options.max_budget_usd = resolve_judge_budget_usd()
    options.output_format = {"type": "json_schema", "schema": _VERDICT_SCHEMA}
    return options


async def _judge_result(prompt: str, options: ClaudeAgentOptions) -> StructuredVerdict | None:
    structured: StructuredVerdict | None = None
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                structured = StructuredVerdict.from_structured_output(message.structured_output)
    except Exception as exc:
        # A SUCCESS mislabeled as an error result (the CLI exited non-zero on a
        # "success" subtype) must not crash the judge: the verdict-bearing
        # ResultMessage already arrived, so return what was captured.
        if not is_success_result_error(str(exc)):
            raise
    return structured


def _verdict_from_structured(structured: StructuredVerdict | None) -> JudgeVerdict:
    """Map the parsed :class:`StructuredVerdict` to a :class:`JudgeVerdict`.

    A judge that returns no usable structured verdict cannot pass a scenario by
    default — an absent/malformed verdict is a FAIL, never a silent pass.
    """
    if structured is None:
        return JudgeVerdict(passed=False, skipped=False, rationale="judge returned no structured verdict")
    rationale = structured.reason if structured.reason and structured.reason.strip() else str(structured.verdict)
    if structured.verdict == "PASS":
        return JudgeVerdict(passed=True, skipped=False, rationale=rationale)
    if structured.verdict == "FAIL":
        return JudgeVerdict(passed=False, skipped=False, rationale=rationale)
    return JudgeVerdict(passed=False, skipped=False, rationale="judge returned no PASS/FAIL verdict")


__all__ = [
    "ClaudeJudge",
    "JudgeBudget",
    "JudgeBudgetExceededError",
    "JudgeVerdict",
    "build_judge_prompt",
]

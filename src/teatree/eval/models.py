"""Frozen dataclasses for the eval harness."""

import dataclasses
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class Matcher:
    """One assertion against captured tool calls.

    ``kind`` is ``"positive"`` (a matching tool call must exist) or
    ``"negative"`` (no matching tool call may exist). ``operator`` is
    ``"contains"`` (substring) or ``"~"`` (regex).
    """

    kind: str
    tool: str
    arg_path: str
    operator: str
    value: str


@dataclasses.dataclass(frozen=True)
class AnyOf:
    """A disjunction of positive matchers — passes when ANY alternative holds.

    Pins a rule that a documented set of equally-valid actions satisfies
    (e.g. "background the long op via a ``Task`` dispatch OR a Bash call
    with ``run_in_background: true``"), so a compliant response taking
    either branch stays green. Restricted to positive alternatives: a
    disjunction of negatives is just one wider negative regex, and mixing
    a forbidden branch into an "any-of" reads ambiguously.
    """

    alternatives: tuple[Matcher, ...]


# An ``expect`` entry is either a single matcher or a disjunction of them.
ExpectItem = Matcher | AnyOf


@dataclasses.dataclass(frozen=True)
class JudgeSpec:
    """Opt-in LLM-judge grading config for a scenario.

    Present only when a scenario's pass/fail is not cleanly matcher-gradeable
    (e.g. "the explanation is faithful to the diff", "the tone is non-blaming").
    A judge model reads the captured transcript and the ``rubric`` and returns
    a PASS/FAIL verdict. ``model`` is the judge tier (defaults to the Sonnet run
    tier) and ``max_output_tokens`` caps the judge's reply — both cost controls.
    """

    rubric: str
    model: str = "claude-sonnet-4-6"
    max_output_tokens: int = 512


@dataclasses.dataclass(frozen=True)
class EvalSpec:
    """A single eval scenario loaded from YAML."""

    name: str
    scenario: str
    agent_path: str
    prompt: str
    matchers: tuple[ExpectItem, ...]
    source_path: Path
    model: str = "claude-sonnet-4-6"
    max_turns: int = 4
    tools: tuple[str, ...] = ("Bash",)
    judge: JudgeSpec | None = None


@dataclasses.dataclass(frozen=True)
class EvalToolCall:
    name: str
    input: dict[str, Any]
    turn: int


@dataclasses.dataclass(frozen=True)
class EvalRun:
    """Captured output of one ``claude -p`` invocation against a spec."""

    spec_name: str
    tool_calls: tuple[EvalToolCall, ...]
    text_blocks: tuple[str, ...]
    terminal_reason: str
    is_error: bool
    raw_stdout: str
    raw_stderr: str
    cost_usd: float = 0.0

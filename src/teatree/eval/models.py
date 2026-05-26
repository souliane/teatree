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
class EvalSpec:
    """A single eval scenario loaded from YAML."""

    name: str
    scenario: str
    agent_path: str
    prompt: str
    matchers: tuple[Matcher, ...]
    source_path: Path
    model: str = "haiku"
    max_turns: int = 4
    tools: tuple[str, ...] = ("Bash",)


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

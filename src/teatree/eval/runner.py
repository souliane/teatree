"""``claude -p`` subprocess runner for behavioral evals.

Shells out to the Claude CLI in ``--output-format stream-json`` mode with
a per-scenario wall-clock watchdog (120s) and a per-invocation budget
circuit breaker (``--max-budget-usd 0.10``). The child runs in a virgin
environment (``--bare`` plus :func:`~teatree.eval.isolation.isolated_claude_env`)
so the developer's ``~/.claude/CLAUDE.md``, auto-memory, and project
``CLAUDE.md`` never bias a result. When ``claude`` is not on PATH the runner
returns a skip-shaped :class:`EvalRun` so the harness can print a clear ``SKIP``
banner and exit 0 — that path is exercised in CI and on contributors who have
not installed the CLI locally.
"""

import dataclasses
import shutil
from pathlib import Path

from teatree.eval.isolation import isolated_claude_env
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.transcript import extract_terminal_reason, extract_text_blocks, extract_tool_calls, parse_stream_json
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

WATCHDOG_SECONDS = 120
MAX_BUDGET_USD = "0.10"
FALLBACK_MODEL = "claude-sonnet-4-6"
EMPTY_SETTINGS = '{"hooks":{}}'


@dataclasses.dataclass(frozen=True)
class _RunnerOutcome:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool


class ClaudeCliMissingError(RuntimeError):
    """Raised when ``claude`` is not on PATH and the caller did not opt in to skip."""


class ClaudePRunner:
    """Run an :class:`EvalSpec` against ``claude -p`` and capture tool calls."""

    def __init__(self, *, workspace: Path | None = None, max_turns_override: int | None = None) -> None:
        self._workspace = workspace or Path.cwd()
        self._max_turns_override = max_turns_override

    def run(self, spec: EvalSpec) -> EvalRun:
        binary = shutil.which("claude")
        if binary is None:
            return self._skip_run(spec, "claude binary not on PATH")

        system_prompt = self._load_agent_definition(spec.agent_path)
        max_turns = self._max_turns_override if self._max_turns_override is not None else spec.max_turns
        command = self._build_command(
            binary,
            spec=spec,
            system_prompt=system_prompt,
            max_turns=max_turns,
        )
        outcome = self._invoke(command)
        if outcome.timed_out:
            return EvalRun(
                spec_name=spec.name,
                tool_calls=(),
                text_blocks=(),
                terminal_reason="timeout",
                is_error=True,
                raw_stdout=outcome.stdout,
                raw_stderr=outcome.stderr,
            )

        events = parse_stream_json(outcome.stdout)
        tool_calls = extract_tool_calls(events)
        text_blocks = extract_text_blocks(events)
        terminal_reason, is_error = extract_terminal_reason(events)
        if outcome.returncode != 0 and terminal_reason == "aborted":
            is_error = True
        return EvalRun(
            spec_name=spec.name,
            tool_calls=tuple(tool_calls),
            text_blocks=tuple(text_blocks),
            terminal_reason=terminal_reason,
            is_error=is_error,
            raw_stdout=outcome.stdout,
            raw_stderr=outcome.stderr,
        )

    def _build_command(
        self,
        binary: str,
        *,
        spec: EvalSpec,
        system_prompt: str,
        max_turns: int,
    ) -> list[str]:
        return [
            binary,
            "-p",
            "--bare",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(max_turns),
            "--max-budget-usd",
            MAX_BUDGET_USD,
            "--model",
            spec.model,
            "--fallback-model",
            FALLBACK_MODEL,
            "--no-session-persistence",
            "--disable-slash-commands",
            "--permission-mode",
            "bypassPermissions",
            "--strict-mcp-config",
            "--tools",
            ",".join(spec.tools),
            "--settings",
            EMPTY_SETTINGS,
            "--add-dir",
            str(self._workspace),
            "--system-prompt",
            system_prompt,
            spec.prompt,
        ]

    @staticmethod
    def _invoke(command: list[str]) -> _RunnerOutcome:
        try:
            with isolated_claude_env() as (env, cwd):
                result = run_allowed_to_fail(
                    command,
                    expected_codes=None,
                    timeout=WATCHDOG_SECONDS,
                    env=env,
                    cwd=cwd,
                )
        except TimeoutExpired as exc:
            return _RunnerOutcome(
                stdout=_coerce_stream(exc.stdout),
                stderr=_coerce_stream(exc.stderr),
                returncode=-1,
                timed_out=True,
            )
        return _RunnerOutcome(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            returncode=result.returncode,
            timed_out=False,
        )

    @staticmethod
    def _load_agent_definition(agent_path: str) -> str:
        resolved = Path(agent_path).expanduser()
        if not resolved.is_absolute():
            for candidate in (Path.cwd() / resolved, _teatree_root() / resolved):
                if candidate.is_file():
                    resolved = candidate
                    break
        if not resolved.is_file():
            msg = f"Agent definition not found: {agent_path}"
            raise FileNotFoundError(msg)
        text = resolved.read_text(encoding="utf-8")
        if not text.strip():
            msg = f"Agent definition is empty: {resolved}"
            raise ValueError(msg)
        return text

    @staticmethod
    def _skip_run(spec: EvalSpec, reason: str) -> EvalRun:
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=(),
            terminal_reason=f"skipped: {reason}",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
        )


def _teatree_root() -> Path:
    """Return the teatree repo root (parent of ``src/teatree``)."""
    return Path(__file__).resolve().parents[3]


def _coerce_stream(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, str):
        return stream
    return stream.decode("utf-8", "replace")

"""``claude -p`` subprocess runner for behavioral evals.

Shells out to the Claude CLI in ``--output-format stream-json`` mode with
a per-scenario wall-clock watchdog (120s) and a per-invocation budget
circuit breaker (``--max-budget-usd 0.10``). The child runs in a virgin
environment via :func:`~teatree.eval.isolation.isolated_claude_env` (``HOME``
redirected at a ``.claude``-free temp dir + neutral cwd) plus the explicit
``--settings``, ``--strict-mcp-config``, ``--system-prompt`` and ``--add-dir``
flags, so the developer's ``~/.claude/CLAUDE.md``, auto-memory, and project
``CLAUDE.md`` never bias a result. The command deliberately does NOT pass
``--bare``: in claude-code 2.x that flag forces "Anthropic auth is strictly
ANTHROPIC_API_KEY … OAuth and keychain are never read", which disables
``CLAUDE_CODE_OAUTH_TOKEN`` auth — the metered lane's only auth (we have no
``sk-ant-api03`` API key), so ``--bare`` silently regressed every metered run to
``$0 / no tool calls``. When ``claude`` is not on PATH the runner
returns a skip-shaped :class:`EvalRun` so the harness can print a clear ``SKIP``
banner and exit 0 — that path is exercised on contributors who have not
installed the CLI locally.

The one exception is ``require_executed``: when the caller has armed the
all-skipped enforcement gate (the metered CI eval job), a missing ``claude``
binary is a provisioning failure, NOT a clean skip. In that mode the runner
raises :class:`ClaudeCliMissingError` on the FIRST scenario instead of emitting
a skip-shaped run — "sdk + require-executed" must never decoratively skip, and
failing at the runner is the earliest possible point (before any scenario is
graded), so a job with the gate armed but no CLI provisioned fails loud rather
than reporting an all-skipped green.
"""

import dataclasses
import shutil
from pathlib import Path

from teatree.eval.context_budget import extract_sections
from teatree.eval.isolation import isolated_claude_env
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.transcript import (
    extract_cost_usd,
    extract_terminal_reason,
    extract_text_blocks,
    extract_tool_calls,
    parse_stream_json,
)
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
    """Raised when ``claude`` is not on PATH while the all-skipped gate is armed.

    ``require_executed`` callers (the metered CI eval job) cannot tolerate a
    decorative skip: a missing binary means the metered backend can execute
    nothing, so the suite would report an all-skipped green. Raising here fails
    the job at the earliest point — before any scenario is graded.
    """


class ClaudePRunner:
    """Run an :class:`EvalSpec` against ``claude -p`` and capture tool calls."""

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        max_turns_override: int | None = None,
        require_executed: bool = False,
    ) -> None:
        self._workspace = workspace or Path.cwd()
        self._max_turns_override = max_turns_override
        self._require_executed = require_executed

    def run(self, spec: EvalSpec) -> EvalRun:
        binary = shutil.which("claude")
        if binary is None:
            if self._require_executed:
                msg = (
                    "claude binary not on PATH but --require-executed is armed: the metered "
                    "sdk backend can execute no scenario, so the suite would report an "
                    "all-skipped green. Provision the Claude CLI (and ANTHROPIC_API_KEY) on "
                    "the runner — sdk + require-executed must never decoratively skip."
                )
                raise ClaudeCliMissingError(msg)
            return self._skip_run(spec, "claude binary not on PATH")

        system_prompt = self._load_agent_definition(spec.agent_path, spec.agent_sections)
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
        cost_usd = extract_cost_usd(events)
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
            cost_usd=cost_usd,
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
    def _load_agent_definition(agent_path: str, agent_sections: tuple[str, ...] = ()) -> str:
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
        if agent_sections:
            return extract_sections(text, agent_sections)
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

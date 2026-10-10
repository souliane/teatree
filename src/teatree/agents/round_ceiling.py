"""A phase task stops at its next round boundary after 90 minutes of work or three pushed rounds.

A shipping task ran four hours of fix / verify-gates / push / red-pipeline rounds against a
moving main, renewing its lease the whole time, so a deploy drain could never finish. The
wall-clock watchdog interrupts mid-work and records a failure; this acts only when a NEW round
starts — a push or ``verify-gates`` — and hands the task off through ``needs_user_input``,
which parks it behind a deferred question.

A runtime that takes hooks gets :meth:`RoundCeiling.pre_tool_use`, which refuses the round
start before it runs and tells the agent to hand off. A runtime without hooks is watched
through its message stream (:meth:`RoundCeiling.observe`) and the driver interrupts it the
moment a round starts past the ceiling, then records the hand-off itself. A push counts as a
round once it has run without error.
"""

import logging
import time
from collections.abc import Callable
from pathlib import PurePath
from typing import cast

from claude_agent_sdk import AssistantMessage, SystemMessage, ToolResultBlock, ToolUseBlock, UserMessage
from claude_agent_sdk.types import HookCallback, HookContext, HookJSONOutput, HookMatcher, PreToolUseHookInput

from teatree.agents.shell_wrapper import program_argv, simple_commands, wrapper_script

logger = logging.getLogger(__name__)

WORK_CEILING_SECONDS = 90 * 60
PUSHED_ROUND_CEILING = 3
ROUND_TOOL_MATCHER = "Bash"
#: The ``SystemMessage`` subtype a hookless harness emits when a shell command STARTS, before
#: its completed tool block arrives; ``data["command"]`` is the command line.
ROUND_STARTED = "command_started"

_GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"})


def _git_subcommand(argv: list[str]) -> str:
    rest = list(argv[1:])
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        if option in _GIT_OPTIONS_WITH_VALUE and rest:
            rest.pop(0)
    return rest[0] if rest else ""


def _t3_words(argv: list[str]) -> list[str]:
    words = []
    for token in argv[1:]:
        if token.startswith("-"):
            break
        words.append(token)
    return words


def _argv_steps(argv: list[str]) -> set[str]:
    if (script := wrapper_script(argv)) is not None:
        return _steps(script)
    program = program_argv(argv)
    if not program:
        return set()
    name = PurePath(program[0]).name
    if name == "git":
        return {"push"} if _git_subcommand(program) == "push" else set()
    if name == "t3":
        return {"push", "verify-gates"} & set(_t3_words(program))
    return set()


def _steps(command: str) -> set[str]:
    return set().union(*(_argv_steps(argv) for argv in simple_commands(command)))


def starts_a_round(command: str) -> bool:
    """Whether *command* pushes or runs ``verify-gates`` — the steps a new round starts with."""
    return bool(_steps(command))


def is_a_push(command: str) -> bool:
    return "push" in _steps(command)


class RoundCeiling:
    """One dispatch's work clock and pushed-round count."""

    def __init__(self, *, enforce_by_observation: bool, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started_at = clock()
        self._enforce_by_observation = enforce_by_observation
        self._pending_pushes: set[str] = set()
        self.pushes = 0
        self.refused = 0
        self.handoff = ""

    async def pre_tool_use(
        self,
        input_data: PreToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        del tool_use_id, context
        if input_data["tool_name"] != ROUND_TOOL_MATCHER:
            return {}
        if not starts_a_round(str(input_data["tool_input"].get("command", ""))):
            return {}
        if breach := self._breach():
            self.refused += 1
            return self._refusal(breach)
        return {}

    @property
    def enforces_by_observation(self) -> bool:
        return self._enforce_by_observation

    def matcher(self) -> HookMatcher:
        return HookMatcher(matcher=ROUND_TOOL_MATCHER, hooks=[cast("HookCallback", self.pre_tool_use)])

    def observe(self, message: object) -> None:
        """Count completed pushes and, on a runtime without hooks, ask for the hand-off as a round starts."""
        if isinstance(message, SystemMessage) and message.subtype == ROUND_STARTED:
            self._round_may_start(str(message.data.get("command", "")))
            return
        if not isinstance(message, AssistantMessage | UserMessage) or not isinstance(message.content, list):
            return
        for block in message.content:
            if isinstance(block, ToolUseBlock) and block.name == ROUND_TOOL_MATCHER:
                command = str(block.input.get("command", ""))
                self._round_may_start(command)
                if is_a_push(command):
                    self._pending_pushes.add(block.id)
            elif isinstance(block, ToolResultBlock) and block.tool_use_id in self._pending_pushes:
                self._pending_pushes.discard(block.tool_use_id)
                if not block.is_error:
                    self.pushes += 1

    def _round_may_start(self, command: str) -> None:
        if self._enforce_by_observation and not self.handoff and starts_a_round(command):
            breach = self._breach()
            if breach:
                self.handoff = f"{breach}; interrupted at the start of round {self.pushes + 1}"
                logger.warning("round ceiling stopped a runtime without hooks: %s", self.handoff)

    def _breach(self) -> str:
        worked = self._clock() - self._started_at
        if worked >= WORK_CEILING_SECONDS:
            return f"this task has worked {worked / 60:.0f} minutes (ceiling {WORK_CEILING_SECONDS // 60})"
        if self.pushes >= PUSHED_ROUND_CEILING:
            return f"this task has already pushed {self.pushes} rounds (ceiling {PUSHED_ROUND_CEILING} rounds)"
        return ""

    def _refusal(self, breach: str) -> HookJSONOutput:
        reason = (
            f"Round ceiling reached: {breach}. Do not start another round. End this run now with "
            "`needs_user_input: true` and a `user_input_reason` that says where the work stands "
            "(branch, last pipeline, what is still red) and what the next round should do; it is "
            "recorded as factory work and nothing resumes on its own."
        )
        logger.warning("round ceiling refused a new round: %s (refused=%d)", breach, self.refused)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
            "systemMessage": f"teatree stopped this task at a round boundary: {breach}.",
        }

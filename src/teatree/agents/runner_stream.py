"""What one headless run's message stream yielded, captured where a ceiling cannot cancel it away.

Split out of :mod:`teatree.agents.runner` (at its module-health LOC cap). The capture lives outside
the ``asyncio.wait_for`` that enforces the runtime ceiling, so a run cut off at that ceiling still
hands over every text block it had already written.
"""

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import RateLimitInfo

from teatree.agents.harness import HarnessSession, pydantic_ai_thread
from teatree.agents.result_schema import AgentResultBlob
from teatree.agents.round_ceiling import RoundCeiling
from teatree.agents.runner_failure_taxonomy import MODEL_FALLBACK_SUBTYPE, TURN_CEILING_SUBTYPE, is_context_exhaustion
from teatree.agents.runner_usage import context_size
from teatree.agents.skill_injection import _bare_skill_name, _resolve_skill_md, harness_skills_dirs

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

#: The tail of a cut-short run's text its failure keeps; a child task's prompt reads the same budget.
KEPT_TEXT_CHARS = 2000


@dataclass(frozen=True)
class HarnessOutcome:
    """The captured result of one in-process harness-driven agent run."""

    agent_text: str
    result_message: ResultMessage | None
    stuck_reason: str | None
    #: The last REJECTED rate-limit window the stream carried (a ``RateLimitEvent``
    #: with ``status == "rejected"``), used to classify a limit failure from the
    #: SDK's unambiguous typed field. ``None`` when the stream named no rejected
    #: window — the classifier then falls back to phrase-matching the result text.
    rate_limit_info: RateLimitInfo | None = None
    #: (#2886) The pydantic_ai session's conversation, ``None`` for every other backend.
    thread: "list[ModelMessage] | None" = None
    #: (#3982) Whether ``stuck_reason`` is a LOST LEASE rather than a watchdog breach. A
    #: typed flag, not a phrase match on the reason: the reason now names the actual
    #: reclaimer, so any discriminator built on its wording would drift with it.
    lease_lost: bool = False
    #: ``ToolUseBlock``s the run emitted. Both backends yield tool use in this same
    #: vocabulary, so the count is lane-agnostic evidence that the agent ACTED —
    #: what :mod:`teatree.agents.action_verification` gates an acting phase on. A
    #: watchdog breach leaves it at the count observed before the breach.
    tool_calls: int = 0
    observed_skill_loads: tuple[str, ...] = ()
    #: Whether the ``PreCompact`` guard ended the run on an automatic compaction attempt.
    compaction_stopped: bool = False
    #: Why the driver stopped a hookless runtime at a round boundary; recorded as a hand-off.
    round_handoff: str = ""
    #: The ``model_fallback`` events the CLI streamed — a run served by a model it did not ask for.
    model_fallbacks: tuple[Mapping[str, object], ...] = ()
    #: The conversation's size at its last main-thread turn; ``None`` when no turn reported usage.
    context_tokens: int | None = None
    #: Whether the run resumed an earlier conversation rather than opening a new one.
    resumed: bool = False

    @property
    def model_fell_back(self) -> bool:
        """Whether the CLI's ``fallback_model`` served any of this run instead of the requested model."""
        return bool(self.model_fallbacks)

    @property
    def cut_short(self) -> bool:
        """Whether a ceiling, a full context window or a blocked compaction ended the run — never a lost lease."""
        if self.lease_lost:
            return False
        message = self.result_message
        return (
            self.compaction_stopped
            or self.stuck_reason is not None
            or (message is not None and message.subtype == TURN_CEILING_SUBTYPE)
            or is_context_exhaustion(message)
        )

    @property
    def unfinished_result(self) -> AgentResultBlob:
        """The text a cut-short run had written, kept on its failure; empty for every other ending."""
        text = self.agent_text.strip()
        return {"summary": text[-KEPT_TEXT_CHARS:]} if self.cut_short and text else {}


@dataclass
class StreamCapture:
    """The stream so far: the agent's text, the terminal result, a rejected window, the tool calls."""

    round_ceiling: RoundCeiling | None = None
    text_parts: list[str] = field(default_factory=list)
    result_message: ResultMessage | None = None
    rate_limit_info: RateLimitInfo | None = None
    tool_calls: int = 0
    observed_skill_loads: list[str] = field(default_factory=list)
    pending_skill_loads: dict[str, tuple[str, Path | None]] = field(default_factory=dict)
    model_fallbacks: list[Mapping[str, object]] = field(default_factory=list)
    context_tokens: int | None = None
    open_tools: dict[str, tuple[str, float]] = field(default_factory=dict)
    last_event_at: float | None = None

    @property
    def open_tool(self) -> tuple[str, float] | None:
        """The longest-running tool call still awaiting its result: its name and monotonic start."""
        return next(iter(self.open_tools.values()), None)

    def observe(self, message: object) -> None:
        self.last_event_at = monotonic()
        if self.round_ceiling is not None:
            self.round_ceiling.observe(message)
        if isinstance(message, AssistantMessage):
            self.text_parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
            self.tool_calls += sum(1 for block in message.content if isinstance(block, ToolUseBlock))
            # A sub-agent's turn is its own conversation; a synthetic error turn reports zero usage.
            if message.parent_tool_use_id is None and (size := context_size(message.usage)):
                self.context_tokens = size
        # The SDK can return tool results in either an assistant or user message;
        # the pydantic seam emits both as assistant messages. A request alone is
        # never evidence that the skill was successfully loaded.
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                self._observe_tool_block(block)
        if isinstance(message, ResultMessage):
            self.result_message = message
        elif isinstance(message, RateLimitEvent) and message.rate_limit_info.status == "rejected":
            self.rate_limit_info = message.rate_limit_info
        elif isinstance(message, SystemMessage) and message.subtype == MODEL_FALLBACK_SUBTYPE:
            self.model_fallbacks.append(message.data)

    def _observe_tool_block(self, block: object) -> None:
        if isinstance(block, ToolUseBlock):
            self.open_tools.setdefault(block.id, (block.name, monotonic()))
        if isinstance(block, ToolResultBlock):
            self.open_tools.pop(block.tool_use_id, None)
            pending = self.pending_skill_loads.pop(block.tool_use_id, None)
            if pending is not None and not block.is_error:
                skill, read_path = pending
                if read_path is None or _complete_skill_read(skill, read_path, block.content):
                    self.observed_skill_loads.append(skill)
        elif isinstance(block, ToolUseBlock) and isinstance(block.input, dict):
            if block.name == "Skill":
                reference = block.input.get("skill")
            elif block.name in {"Read", "read_file"}:
                path = block.input.get("file_path") or block.input.get("path")
                reference = path if isinstance(path, str) and path.endswith("/SKILL.md") else None
            elif block.name == "Bash":
                reference = _direct_skill_cat_path(block.input.get("command"))
            else:
                return
            if isinstance(reference, str) and reference:
                self.pending_skill_loads[block.id] = (
                    _bare_skill_name(reference),
                    Path(reference) if block.name in {"Read", "read_file", "Bash"} else None,
                )

    def outcome(self, *, stuck_reason: str | None = None, thread: "list[ModelMessage] | None" = None) -> HarnessOutcome:
        return HarnessOutcome(
            agent_text="\n".join(self.text_parts),
            result_message=self.result_message,
            stuck_reason=stuck_reason,
            rate_limit_info=self.rate_limit_info,
            thread=thread,
            tool_calls=self.tool_calls,
            observed_skill_loads=tuple(dict.fromkeys(self.observed_skill_loads)),
            round_handoff=self.round_handoff,
            model_fallbacks=tuple(self.model_fallbacks),
            context_tokens=self.context_tokens,
        )

    @property
    def round_handoff(self) -> str:
        return self.round_ceiling.handoff if self.round_ceiling is not None else ""


async def _collect(session: HarnessSession, prompt: str, capture: StreamCapture | None = None) -> HarnessOutcome:
    """Send *prompt* and collect the agent's text + terminal ``ResultMessage`` + rejected window."""
    capture = capture if capture is not None else StreamCapture()
    await session.query(prompt)
    interrupted = False
    async for message in session.receive_response():
        capture.observe(message)
        if capture.round_handoff and not interrupted:
            await session.interrupt()
            interrupted = True
    return capture.outcome(thread=pydantic_ai_thread(session))  # (#2886) captured while `session` is still open


def _complete_skill_read(skill: str, read_path: Path, content: object) -> bool:
    """A successful Read counts only if it returned the full configured skill body."""
    try:
        expected = _resolve_skill_md(skill, harness_skills_dirs())
        if expected is None or read_path.resolve() != expected.resolve() or not isinstance(content, str):
            return False
        body = expected.read_text(encoding="utf-8")
    except (OSError, RuntimeError, UnicodeError):
        return False
    return bool(body) and body in content


def _direct_skill_cat_path(command: object) -> str | None:
    """Recognize only a single, direct full-file shell read as potential evidence.

    Codex App Server reports shell reads as ``commandExecution``/``Bash``. An
    arbitrary command mentioning a skill path is not proof it loaded the body;
    its result still has to pass :func:`_complete_skill_read`.
    """
    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    match tokens:
        case ["cat", "--", path] | ["cat", path]:
            pass
        case _:
            return None
    return path if Path(path).is_absolute() and Path(path).name == "SKILL.md" else None

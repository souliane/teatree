"""Run shipped PreToolUse hooks on the CLI-free eval tool transport.

The Claude-SDK eval backend installs ``hooks/hooks.json`` as a local plugin.  The
direct ``pydantic_ai`` / Anthropic backend has no plugin host, so its inert tool
stubs previously bypassed every production hook even when a scenario declared
``production_hooks: true``.  This module is the transport adapter: it reads the
same shipped manifest, presents the hook with a Claude-shaped live transcript,
and refuses a wrapped tool with ``ModelRetry`` before its body executes.

Only PreToolUse is bridged here.  That is the lifecycle point which can prevent
an action; synthesising SessionStart/Stop side effects in the direct-model
runner would claim parity that the transport cannot provide.  Each completed
invocation is nevertheless emitted as the same ``HookEventMessage`` vocabulary
the SDK runner records, so the evaluator can prove the gate actually ran.
"""

import json
import os
import re
import shlex
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from claude_agent_sdk.types import HookEventMessage
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)

from teatree.eval.production_hook_toolset import ProductionHookToolset
from teatree.eval.production_hooks import PLUGIN_ROOT_VAR, hooked_env, preflighted_plugin_root
from teatree.eval.under_load import build_user_prompt
from teatree.llm.credentials import AnthropicApiKeyCredential
from teatree.utils.run import SUBPROCESS_UNREACHABLE, redact_secrets, run_allowed_to_fail

if TYPE_CHECKING:
    from collections.abc import Iterator

    from teatree.agents.pydantic_ai_turn import SessionRun
    from teatree.eval.models import EvalSpec

_EVENT = "PreToolUse"
_DENY_EXIT = 2
_OUTPUT_CAP = 4000
_ASSISTANT_TEXT_CAP = 4000


class _TranscriptBlock(TypedDict, total=False):
    type: str
    text: str
    id: str
    name: str
    input: dict[str, Any]
    tool_use_id: str
    content: object
    is_error: bool


class _TranscriptMessage(TypedDict):
    role: str
    content: list[_TranscriptBlock]


class _TranscriptEntry(TypedDict):
    type: str
    message: _TranscriptMessage


class _AuditContext(TypedDict):
    sequence: int
    tool_name: str
    tool_use_id: str
    assistant_text: str


@dataclass(slots=True)
class ProductionHookBridge:
    """One direct-model eval run's live transcript and shipped-hook events."""

    prompt: str
    run: "SessionRun"
    cwd: Path
    state_root: Path
    plugin_root: Path
    events: list[HookEventMessage] = field(default_factory=list)
    _entries: list[_TranscriptEntry] = field(default_factory=list)
    _call_sequence: int = 0

    def __post_init__(self) -> None:
        self._entries.append(
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": self.prompt}]}}
        )

    @classmethod
    @contextmanager
    def for_spec(
        cls,
        spec: "EvalSpec",
        *,
        run: "SessionRun",
        cwd: Path | None = None,
        state_root: Path | None = None,
    ) -> "Iterator[ProductionHookBridge]":
        """Provision isolated hook state for *spec* and remove it after the run."""
        plugin_root = preflighted_plugin_root()
        if state_root is not None:
            state_root.mkdir(parents=True, exist_ok=True)
            yield cls(build_user_prompt(spec), run, cwd or state_root, state_root, plugin_root)
            return
        with tempfile.TemporaryDirectory(prefix="t3-eval-hooks-") as raw:
            root = Path(raw)
            yield cls(build_user_prompt(spec), run, cwd or root, root, plugin_root)

    @property
    def transcript_path(self) -> Path:
        return self.state_root / "transcript.jsonl"

    @property
    def saw_gated_calls(self) -> bool:
        """Whether any tool call reached the gate — incremented before a command matches.

        This lane fires a hook only from a tool call, so it is what separates a
        plugin that never registered from a trajectory that never called a tool:
        both capture zero hook events.
        """
        return self._call_sequence > 0

    def observe(self, event: object) -> None:
        """Capture streamed visible text before pydantic executes a tool call."""
        if isinstance(event, PartStartEvent):
            content = self._assistant_content(start_new=event.index == 0)
            if isinstance(event.part, TextPart):
                content.append({"type": "text", "text": event.part.content})
            elif isinstance(event.part, ToolCallPart):
                self._remember_tool(event.part.tool_name, event.part.args_as_dict(), event.part.tool_call_id)
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
            content = self._assistant_content()
            for block in reversed(content):
                if block.get("type") == "text":
                    block["text"] = f"{block.get('text', '')}{event.delta.content_delta}"
                    break
            else:
                content.append({"type": "text", "text": event.delta.content_delta})
        elif isinstance(event, FunctionToolCallEvent):
            part = event.part
            self._remember_tool(part.tool_name, part.args_as_dict(), part.tool_call_id)

    def pre_tool_use(self, name: str, tool_args: dict[str, Any], *, tool_call_id: str | None = None) -> str | None:
        """Run every matching shipped PreToolUse command; return a denial reason."""
        self._call_sequence += 1
        call_id = tool_call_id or f"direct-{self._call_sequence}"
        self._remember_tool(name, tool_args, call_id)
        self._write_transcript()
        audit_context: _AuditContext = {
            "sequence": self._call_sequence,
            "tool_name": name,
            "tool_use_id": call_id,
            "assistant_text": self._current_assistant_text(),
        }
        payload = {
            "session_id": self.run.session_id,
            "hook_event_name": _EVENT,
            "cwd": str(self.cwd),
            "transcript_path": str(self.transcript_path),
            "tool_name": name,
            "tool_input": tool_args,
            "tool_use_id": call_id,
        }
        for command, timeout in self._matching_commands(name):
            try:
                result = run_allowed_to_fail(
                    command,
                    expected_codes=None,
                    stdin_text=json.dumps(payload),
                    timeout=timeout,
                    cwd=self.cwd,
                    env=self._hook_env(),
                )
            except SUBPROCESS_UNREACHABLE as exc:
                # Claude's command-hook host treats a crashed/timed-out hook as a
                # hook error, not as a permission denial.  Preserve that fail-open
                # posture while making the degraded invocation visible to the eval.
                self.events.append(
                    HookEventMessage(
                        subtype="hook_response",
                        hook_event_name=_EVENT,
                        session_id=self.run.session_id,
                        data={
                            "hook_event": _EVENT,
                            "outcome": "error",
                            "output": redact_secrets(f"{type(exc).__name__}: {exc}")[:_OUTPUT_CAP],
                            "exit_code": None,
                            **audit_context,
                        },
                    )
                )
                continue
            parsed = _last_json_object(result.stdout)
            decision = parsed.get("hookSpecificOutput", {}) if parsed else {}
            denied = result.returncode == _DENY_EXIT or decision.get("permissionDecision") == "deny"
            reason = str(decision.get("permissionDecisionReason", "")).strip()
            gate_id = str(decision.get("gate_id", "")).strip()
            output = (result.stdout or result.stderr).strip()[:_OUTPUT_CAP]
            data: dict[str, Any] = {
                "hook_event": _EVENT,
                "outcome": _outcome(denied=denied, exit_code=result.returncode),
                "output": redact_secrets(output),
                "exit_code": result.returncode,
                "reason": redact_secrets(reason),
                **audit_context,
            }
            if gate_id:
                data["gate_id"] = gate_id
            self.events.append(
                HookEventMessage(
                    subtype="hook_response",
                    hook_event_name=_EVENT,
                    session_id=self.run.session_id,
                    data=data,
                )
            )
            if denied:
                return reason or "The shipped PreToolUse hook denied this tool call."
        return None

    def _current_assistant_text(self) -> str:
        """User-visible text in the response that requested the governed tool."""
        if not self._entries or self._entries[-1].get("type") != "assistant":
            return ""
        text = "\n".join(
            str(block.get("text", "")) for block in _entry_content(self._entries[-1]) if block.get("type") == "text"
        )
        return redact_secrets(text)[:_ASSISTANT_TEXT_CAP]

    def _remember_tool(self, name: str, tool_args: dict[str, Any], tool_call_id: str) -> None:
        if any(
            block.get("id") == tool_call_id
            for entry in self._entries
            for block in _entry_content(entry)
            if block.get("type") == "tool_use"
        ):
            return
        self._assistant_content().append({"type": "tool_use", "id": tool_call_id, "name": name, "input": tool_args})

    def record_tool_result(self, tool_call_id: str, content: object, *, is_error: bool = False) -> None:
        """Append the tool-result pseudo-user entry the Claude transcript carries."""
        block: _TranscriptBlock = {
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": str(content),
        }
        if is_error:
            block["is_error"] = True
        self._entries.append({"type": "user", "message": {"role": "user", "content": [block]}})

    def _assistant_content(self, *, start_new: bool = False) -> list[_TranscriptBlock]:
        if start_new or not self._entries or self._entries[-1].get("type") != "assistant":
            self._entries.append({"type": "assistant", "message": {"role": "assistant", "content": []}})
        return _entry_content(self._entries[-1])

    def _write_transcript(self) -> None:
        self.transcript_path.write_text(
            "\n".join(json.dumps(entry) for entry in self._entries),
            encoding="utf-8",
        )

    def flush_transcript(self) -> None:
        """Persist the complete transcript after the final model response."""
        self._write_transcript()

    def _matching_commands(self, tool_name: str) -> "Iterator[tuple[list[str], int]]":
        manifest = json.loads((self.plugin_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        for entry in manifest.get("hooks", {}).get(_EVENT, []):
            matcher = str(entry.get("matcher", ""))
            if matcher and re.fullmatch(matcher, tool_name) is None:
                continue
            for hook in entry.get("hooks", []):
                if hook.get("type") != "command":
                    continue
                raw = str(hook.get("command", "")).replace(f"${{{PLUGIN_ROOT_VAR}}}", str(self.plugin_root))
                if raw:
                    yield shlex.split(raw), int(hook.get("timeout", 30))

    def _hook_env(self) -> dict[str, str]:
        credential = AnthropicApiKeyCredential().spec
        stripped = {credential.env_var, *credential.conflicting_vars}
        env = {key: value for key, value in os.environ.items() if key not in stripped}
        env["HOME"] = str(self.state_root)
        env["XDG_CONFIG_HOME"] = str(self.state_root / ".config")
        env["CLAUDE_CONFIG_DIR"] = str(self.state_root / ".claude")
        env["T3_HOOK_PYTHON"] = sys.executable
        return hooked_env(env, str(self.state_root))


def _outcome(*, denied: bool, exit_code: int) -> str:
    """How the hook invocation ended — a verdict, or no verdict at all.

    Only 0 and the deny exit are verdicts; anything else is a hook that crashed
    or refused to run, and scoring that as ``allow`` reads a sanctioned pass out
    of a gate that never examined the call.
    """
    if denied:
        return "block"
    return "allow" if exit_code == 0 else "error"


def _last_json_object(output: str) -> dict[str, Any]:
    """Last JSON object a hook printed; non-JSON diagnostics are ignored."""
    for line in reversed(output.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _entry_content(entry: _TranscriptEntry) -> list[_TranscriptBlock]:
    """Mutable content list from one synthetic transcript entry."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


__all__ = ["ProductionHookBridge", "ProductionHookToolset"]

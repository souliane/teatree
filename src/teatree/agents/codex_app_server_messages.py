"""Translate Codex thread items and usage into TeaTree's neutral SDK stream."""

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ContentBlock,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from teatree.agents.codex_app_server_errors import turn_error
from teatree.agents.codex_app_server_options import CodexAppServerError
from teatree.agents.round_ceiling import ROUND_STARTED

if TYPE_CHECKING:
    from claude_agent_sdk.types import ModelUsage


def tool_blocks(item: Mapping[str, Any]) -> list[ContentBlock]:
    item_id = str(item.get("id", ""))
    item_type = item.get("type")
    if item_type == "commandExecution":
        tool = ToolUseBlock(
            id=item_id,
            name="Bash",
            input={"command": str(item.get("command", "")), "cwd": str(item.get("cwd", ""))},
        )
        result = ToolResultBlock(
            tool_use_id=item_id,
            content=str(item.get("aggregatedOutput") or ""),
            is_error=item.get("status") == "failed" or item.get("exitCode") not in {None, 0},
        )
        return [tool, result]
    if item_type == "fileChange":
        tool = ToolUseBlock(id=item_id, name="Edit", input={"changes": item.get("changes", [])})
        result = ToolResultBlock(
            tool_use_id=item_id,
            content=str(item.get("status", "")),
            is_error=item.get("status") == "failed",
        )
        return [tool, result]
    if item_type == "mcpToolCall":
        arguments = item.get("arguments")
        tool = ToolUseBlock(
            id=item_id,
            name=f"mcp__{item.get('server', '')}__{item.get('tool', '')}",
            input=arguments if isinstance(arguments, dict) else {"value": arguments},
        )
        error = item.get("error")
        content = error if error is not None else item.get("result")
        result = ToolResultBlock(
            tool_use_id=item_id,
            content=json.dumps(content, default=str),
            is_error=error is not None,
        )
        return [tool, result]
    if item_type == "collabAgentToolCall":
        input_payload = {
            key: item[key]
            for key in ("tool", "model", "reasoningEffort", "receiverThreadIds")
            if item.get(key) is not None
        }
        tool = ToolUseBlock(id=item_id, name="Agent", input=input_payload)
        result = ToolResultBlock(
            tool_use_id=item_id,
            content=json.dumps(item.get("agentsStates", {}), default=str),
            is_error=item.get("status") == "failed",
        )
        return [tool, result]
    if item_type in {"subAgentActivity", "dynamicToolCall", "imageGeneration"}:
        return _auxiliary_tool_blocks(item, item_id=item_id, item_type=str(item_type))
    return []


def _auxiliary_tool_blocks(item: Mapping[str, Any], *, item_id: str, item_type: str) -> list[ContentBlock]:
    if item_type == "subAgentActivity":
        tool = ToolUseBlock(
            id=item_id,
            name="AgentActivity",
            input={key: item.get(key) for key in ("agentThreadId", "agentPath", "kind")},
        )
        result = ToolResultBlock(tool_use_id=item_id, content=str(item.get("kind", "")), is_error=False)
        return [tool, result]
    if item_type == "dynamicToolCall":
        arguments = item.get("arguments")
        tool = ToolUseBlock(
            id=item_id,
            name=str(item.get("tool") or "DynamicTool"),
            input=arguments if isinstance(arguments, dict) else {"value": arguments},
        )
        error = item.get("error")
        result = ToolResultBlock(
            tool_use_id=item_id,
            content=json.dumps(error if error is not None else item.get("result"), default=str),
            is_error=error is not None or item.get("status") == "failed",
        )
        return [tool, result]
    if item_type == "imageGeneration":
        tool = ToolUseBlock(
            id=item_id,
            name="ImageGeneration",
            input={key: item[key] for key in ("prompt", "revisedPrompt") if item.get(key) is not None},
        )
        result = ToolResultBlock(
            tool_use_id=item_id,
            content=json.dumps(
                {key: item[key] for key in ("result", "savedPath") if item.get(key) is not None},
                default=str,
            ),
            is_error=item.get("status") == "failed" or item.get("error") is not None,
        )
        return [tool, result]
    return []


def translate_usage(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("last"), dict):
        return None
    last = value["last"]
    return {
        "input_tokens": last.get("inputTokens"),
        "output_tokens": last.get("outputTokens"),
        "cache_read_input_tokens": last.get("cachedInputTokens"),
        "cache_creation_input_tokens": last.get("cacheWriteInputTokens"),
    }


class CodexEventTranslator:
    """One session's turn state: what each turn's events have said so far."""

    def __init__(self) -> None:
        self.side_effects_started = False
        self._text_by_item: dict[str, list[str]] = {}
        self._emitted_items: set[str] = set()
        self._text: list[str] = []
        self._usage: dict[str, Any] | None = None

    def reset(self) -> None:
        self.side_effects_started = False
        self._text_by_item.clear()
        self._emitted_items.clear()
        self._text.clear()
        self._usage = None

    def translate(self, event: Mapping[str, Any], *, thread_id: str, model: str) -> tuple[list[object], bool]:
        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, dict) or params.get("threadId") != thread_id:
            return [], False
        if method == "item/agentMessage/delta":
            self._record_delta(params)
        elif method == "item/started":
            item = params.get("item")
            self.record_possible_side_effect(item)
            if isinstance(item, dict) and item.get("type") == "commandExecution":
                return [SystemMessage(subtype=ROUND_STARTED, data={"command": str(item.get("command", ""))})], False
        elif method == "item/completed":
            self.record_possible_side_effect(params.get("item"))
            message = self._completed_item_message(params.get("item"), model)
            return ([message] if message is not None else []), False
        elif method == "thread/tokenUsage/updated":
            self._usage = translate_usage(params.get("tokenUsage"))
        elif method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise CodexAppServerError.missing_turn()
            if turn.get("error") is not None:
                raise turn_error(
                    turn.get("error"),
                    side_effects_started=self.side_effects_started,
                    agent_session_id=thread_id,
                )
            return [*self._remaining_turn_messages(turn, model), self._result_message(turn, thread_id, model)], True
        return [], False

    def _record_delta(self, params: Mapping[str, Any]) -> None:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if isinstance(item_id, str) and isinstance(delta, str):
            self._text_by_item.setdefault(item_id, []).append(delta)

    def record_possible_side_effect(self, value: object) -> None:
        if isinstance(value, dict) and value.get("type") in {
            "collabAgentToolCall",
            "commandExecution",
            "fileChange",
            "mcpToolCall",
            "subAgentActivity",
            "dynamicToolCall",
            "imageGeneration",
        }:
            self.side_effects_started = True

    def _completed_item_message(self, value: object, model: str) -> AssistantMessage | None:
        if not isinstance(value, dict):
            return None
        item_id = str(value.get("id", ""))
        if not item_id or item_id in self._emitted_items:
            return None
        self._emitted_items.add(item_id)
        item_type = value.get("type")
        if item_type == "agentMessage":
            text = str(value.get("text") or "".join(self._text_by_item.get(item_id, ())))
            if not text:
                return None
            self._text.append(text)
            return AssistantMessage(content=[TextBlock(text=text)], model=model)
        blocks = tool_blocks(value)
        return AssistantMessage(content=blocks, model=model) if blocks else None

    def _remaining_turn_messages(self, turn: Mapping[str, Any], model: str) -> list[AssistantMessage]:
        messages: list[AssistantMessage] = []
        items = turn.get("items")
        if isinstance(items, list):
            for item in items:
                message = self._completed_item_message(item, model)
                if message is not None:
                    messages.append(message)
        for item_id, chunks in self._text_by_item.items():
            if item_id not in self._emitted_items and chunks:
                text = "".join(chunks)
                self._emitted_items.add(item_id)
                self._text.append(text)
                messages.append(AssistantMessage(content=[TextBlock(text=text)], model=model))
        return messages

    def _result_message(self, turn: Mapping[str, Any], thread_id: str, model: str) -> ResultMessage:
        status = str(turn.get("status", ""))
        is_error = status != "completed"
        error = turn.get("error")
        detail = error.get("message") if isinstance(error, dict) else None
        duration = turn.get("durationMs")
        duration_ms = int(duration) if isinstance(duration, int | float) else 0
        model_usage = cast("dict[str, ModelUsage]", {model: {}})
        return ResultMessage(
            subtype="success" if not is_error else "error_during_execution",
            duration_ms=duration_ms,
            duration_api_ms=duration_ms,
            is_error=is_error,
            num_turns=1,
            session_id=thread_id,
            total_cost_usd=None,
            usage=self._usage,
            result=str(detail) if detail else "".join(self._text),
            model_usage=model_usage,
        )

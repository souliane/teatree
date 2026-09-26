"""Translate Codex thread items and usage into TeaTree's neutral SDK stream."""

import json
from collections.abc import Mapping
from typing import Any

from claude_agent_sdk import ContentBlock, ToolResultBlock, ToolUseBlock


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

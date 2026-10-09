"""Decode the JSON a `call_tool` result carries.

Four test modules each carried their own copy of this decoder, so a change to
mcp's return type broke all four at once. One decoder means the next change is
one edit.

`MCPServer.call_tool` returns `CallToolResult | InputRequiredResult`; only the
former carries tool output, and the discriminator is `result_type`. Decoding an
elicitation result is a caller error, not another shape to unwrap — a pydantic
model iterates and `getattr`s without complaint, so an undiscriminated decode
returns plausible nonsense instead of failing.
"""

import json
from typing import Any

from mcp.types import CallToolResult


class NotACompletedToolResultError(TypeError):
    def __init__(self, result: Any) -> None:
        described = getattr(result, "result_type", None) or type(result).__name__
        super().__init__(f"call_tool returned {described}, which carries no tool output")


def _completed(result: Any) -> CallToolResult:
    """*result* as a completed tool result, or a loud failure."""
    if not isinstance(result, CallToolResult) or result.result_type != "complete":
        raise NotACompletedToolResultError(result)
    return result


def content_blocks(result: Any) -> list[Any]:
    """The content blocks of *result*."""
    return list(_completed(result).content)


def payloads(result: Any) -> list[Any]:
    """Every JSON payload decoded from *result*'s text blocks, in order."""
    return [json.loads(text) for block in content_blocks(result) if (text := getattr(block, "text", None)) is not None]


def structured(result: Any) -> Any:
    """The structured output of *result*, unwrapped from its single-key envelope.

    A tool returning a bare list/scalar is wrapped by mcp as ``{"result": ...}``;
    callers assert against the value itself, so the envelope is peeled here
    rather than in every test.
    """
    value = _completed(result).structured_content
    if isinstance(value, dict) and set(value) == {"result"}:
        return value["result"]
    return value

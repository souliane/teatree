"""Decode the JSON a `call_tool` result carries, across every shape mcp has used.

Four test modules each carried their own copy of this decoder, so the mcp 2.0
return-type change (a `CallToolResult` object where 1.x yielded a bare block
list or a `(blocks, ...)` tuple) broke all four at once. One decoder means the
next shape change is one edit.
"""

import json
from typing import Any


def content_blocks(result: Any) -> list[Any]:
    """The content blocks of *result*, whichever container mcp wrapped them in."""
    blocks = getattr(result, "content", None)
    if blocks is not None:
        return list(blocks)
    if isinstance(result, tuple):
        return list(result[0])
    return list(result)


def payloads(result: Any) -> list[Any]:
    """Every JSON payload decoded from *result*'s text blocks, in order."""
    return [json.loads(text) for block in content_blocks(result) if (text := getattr(block, "text", None)) is not None]


def structured(result: Any) -> Any:
    """The structured output of *result*, unwrapped from its single-key envelope.

    A tool returning a bare list/scalar is wrapped by mcp as ``{"result": ...}``;
    callers assert against the value itself, so the envelope is peeled here
    rather than in every test.
    """
    value = getattr(result, "structured_content", None)
    if value is None:
        value = result[1] if isinstance(result, tuple) else result
    if isinstance(value, dict) and set(value) == {"result"}:
        return value["result"]
    return value

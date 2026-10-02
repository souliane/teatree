"""Wire-shaped replies from a metered OpenAI-compatible router, for tests that fake only the network."""

import json

import httpx2

RESOLVED_MODEL = "z-ai/glm-5.3-flash"
ROUTER_HEADERS = {
    "X-Orca-Resolved-Model": RESOLVED_MODEL,
    "X-Orca-Router": "teatree-auto",
    "X-Orca-Request-Id": "req-1",
}


def _chunk(**fields: object) -> dict[str, object]:
    return {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "glm-5.3-flash", **fields}


def _usage_chunk(*, prompt: int, cached: int, cost_usd: float) -> dict[str, object]:
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": 5,
        "total_tokens": prompt + 5,
        "prompt_tokens_details": {"cached_tokens": cached},
        "cost_usd": cost_usd,
    }
    return _chunk(choices=[], usage=usage)


def _event_stream(*chunks: dict[str, object]) -> httpx2.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    headers = {"content-type": "text/event-stream", **ROUTER_HEADERS}
    return httpx2.Response(200, headers=headers, content=body.encode())


def text_reply(text: str, *, cost_usd: float) -> httpx2.Response:
    return _event_stream(
        _chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        _usage_chunk(prompt=21870, cached=21824, cost_usd=cost_usd),
    )


def tool_call_reply(tool_name: str, *, cost_usd: float, arguments: str = "{}") -> httpx2.Response:
    call = {"index": 0, "id": "call_1", "type": "function", "function": {"name": tool_name, "arguments": arguments}}
    return _event_stream(
        _chunk(choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}, "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
        _usage_chunk(prompt=100, cached=0, cost_usd=cost_usd),
    )


def spend_stop() -> httpx2.Response:
    return httpx2.Response(403, json={"error": {"message": "token cycle spend limit reached", "type": "access_denied"}})

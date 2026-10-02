"""The usage tee reads a metered router's per-request usage straight off the wire.

pydantic_ai keeps only integer usage details, so a router's float ``cost_usd`` never reaches
``RunUsage``. The tee sits on the HTTP client and records it before any library drops it.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import httpx2
import pytest

from teatree.llm.usage_tee import UsageTee

_RESOLVED = {"X-Orca-Resolved-Model": "z-ai/glm-5.3-flash"}
_ROUTED = {
    **_RESOLVED,
    "X-Orca-Router": "teatree-auto",
    "X-Orca-Request-Id": "20260915-abc",
    "X-Orca-Session-Tier": "strong; reason=difficulty",
    "X-Orca-Fallback-Level": "1",
    "X-Orca-Fallback-Model": "deepseek/deepseek-v4.1-flash",
}
_NO_ROUTING = {"router": "", "request_id": "", "session_tier": "", "fallback_level": "", "fallback_model": ""}
_USAGE = {"prompt_tokens": 21870, "completion_tokens": 8, "prompt_tokens_details": {"cached_tokens": 21824}}


def _sse(*payloads: object) -> bytes:
    frames = [f"data: {json.dumps(payload)}\n\n" for payload in payloads]
    return ("".join(frames) + "data: [DONE]\n\n").encode()


class _ChunkedStream(httpx2.AsyncByteStream):
    def __init__(self, body: bytes, *, size: int) -> None:
        self._chunks = [body[offset : offset + size] for offset in range(0, len(body), size)]

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def _post_all(tee: UsageTee, handler: Callable[[httpx2.Request], httpx2.Response], *, count: int = 1) -> list[bytes]:
    async def send() -> list[bytes]:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler), event_hooks={"response": [tee.capture]}
        ) as client:
            return [(await client.post("https://router.example/v1/chat/completions")).content for _ in range(count)]

    return asyncio.run(send())


class TestUsageTee:
    def test_a_streamed_reply_records_the_float_cost_tokens_and_resolved_model(self) -> None:
        body = _sse(
            {"choices": [{"delta": {"content": "OK"}}]}, {"choices": [], "usage": {**_USAGE, "cost_usd": 0.000382}}
        )
        tee = UsageTee()

        _post_all(
            tee,
            lambda _r: httpx2.Response(200, headers={"content-type": "text/event-stream", **_RESOLVED}, content=body),
        )

        [request] = tee.requests
        assert request.cost_usd == pytest.approx(0.000382)
        assert request.as_record() == {
            "model": "z-ai/glm-5.3-flash",
            **_NO_ROUTING,
            "prompt_tokens": 21870,
            "completion_tokens": 8,
            "cached_tokens": 21824,
            "cost_usd": 0.000382,
        }

    def test_a_usage_frame_split_across_network_chunks_is_still_read(self) -> None:
        body = _sse({"choices": [], "usage": {**_USAGE, "cost_usd": 0.001642}})
        tee = UsageTee()

        replies = _post_all(
            tee,
            lambda _r: httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=_ChunkedStream(body, size=7)
            ),
        )

        assert replies == [body], "the consumer still receives every byte unchanged"
        assert tee.requests[0].cost_usd == pytest.approx(0.001642)

    def test_a_non_streamed_json_reply_is_read_the_same_way(self) -> None:
        payload = {"choices": [{"message": {"content": "OK"}}], "usage": {**_USAGE, "cost_usd": 0.00004}}
        tee = UsageTee()

        _post_all(tee, lambda _r: httpx2.Response(200, headers=_RESOLVED, json=payload))

        assert tee.requests[0].cost_usd == pytest.approx(0.00004)
        assert tee.requests[0].as_record()["model"] == "z-ai/glm-5.3-flash"

    def test_each_request_gets_its_own_record_in_order(self) -> None:
        costs = iter((0.001642, 0.000382))

        def reply(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json={"usage": {**_USAGE, "cost_usd": next(costs)}})

        tee = UsageTee()
        _post_all(tee, reply, count=2)

        assert [request.cost_usd for request in tee.requests] == [pytest.approx(0.001642), pytest.approx(0.000382)]

    def test_a_reply_without_a_cost_field_records_no_cost(self) -> None:
        tee = UsageTee()

        _post_all(tee, lambda _r: httpx2.Response(200, json={"usage": _USAGE}))

        assert tee.requests[0].cost_usd is None
        assert tee.requests[0].as_record()["prompt_tokens"] == 21870

    def test_a_refused_request_is_recorded_without_usage(self) -> None:
        refusal = {"error": {"message": "token cycle spend limit reached", "type": "access_denied"}}
        tee = UsageTee()

        _post_all(tee, lambda _r: httpx2.Response(403, json=refusal))

        [request] = tee.requests
        assert request.cost_usd is None
        assert request.as_record() == {
            "model": "",
            **_NO_ROUTING,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cached_tokens": None,
            "cost_usd": None,
        }

    def test_every_documented_routing_header_is_kept_on_the_record(self) -> None:
        tee = UsageTee()

        _post_all(tee, lambda _r: httpx2.Response(200, headers=_ROUTED, json={"usage": _USAGE}))

        record = tee.requests[0].as_record()
        assert record["model"] == "z-ai/glm-5.3-flash"
        assert record["router"] == "teatree-auto"
        assert record["request_id"] == "20260915-abc"
        assert record["session_tier"] == "strong; reason=difficulty"
        assert record["fallback_level"] == "1"
        assert record["fallback_model"] == "deepseek/deepseek-v4.1-flash"

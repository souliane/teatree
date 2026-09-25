"""Record a metered router's per-request usage straight off the wire.

pydantic_ai maps a response's ``usage`` into ``RunUsage`` keeping only integer details, so a
router's float ``cost_usd`` and the ``X-Orca-Resolved-Model`` header never reach the run.
:meth:`UsageTee.capture` is an httpx2 response hook: it reads the last ``usage`` object each response
carries, streamed (``data:`` frames) or not, from a body already loaded or by wrapping the stream,
without changing a byte the client consumes.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TypedDict

import httpx2

#: The usage keys a metered OpenAI-compatible endpoint may carry its own per-request cost under.
ROUTER_COST_KEYS = ("cost", "cost_usd", "total_cost", "total_cost_usd")
RESOLVED_MODEL_HEADER = "X-Orca-Resolved-Model"
#: OrcaRouter's other documented routing headers (docs.orcarouter.ai/routing/response-headers), by record key.
ROUTING_HEADERS = {
    "router": "X-Orca-Router",
    "request_id": "X-Orca-Request-Id",
    "session_tier": "X-Orca-Session-Tier",
    "fallback_level": "X-Orca-Fallback-Level",
    "fallback_model": "X-Orca-Fallback-Model",
}
_EVENT_STREAM = "text/event-stream"
_DATA_PREFIX = b"data:"
_DONE = b"[DONE]"


type UsagePayload = dict[str, object]


class RequestRecord(TypedDict):
    """One request as persisted in ``TaskAttempt.result["usage_per_request"]``."""

    model: str
    router: str
    request_id: str
    session_tier: str
    fallback_level: str
    fallback_model: str
    prompt_tokens: int | float | None
    completion_tokens: int | float | None
    cached_tokens: int | float | None
    cost_usd: float | None


def _number(value: object) -> int | float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


@dataclass
class RequestUsage:
    """One HTTP request's reported usage: the model that actually ran, how it was routed, and the last usage seen."""

    resolved_model: str = ""
    routing: dict[str, str] = field(default_factory=dict)
    usage: UsagePayload = field(default_factory=dict)

    @property
    def cost_usd(self) -> float | None:
        for key in ROUTER_COST_KEYS:
            cost = _number(self.usage.get(key))
            if cost is not None and cost >= 0:
                return float(cost)
        return None

    def as_record(self) -> RequestRecord:
        details = self.usage.get("prompt_tokens_details")
        cached = _number(details.get("cached_tokens")) if isinstance(details, dict) else None
        return RequestRecord(
            model=self.resolved_model,
            router=self.routing.get("router", ""),
            request_id=self.routing.get("request_id", ""),
            session_tier=self.routing.get("session_tier", ""),
            fallback_level=self.routing.get("fallback_level", ""),
            fallback_model=self.routing.get("fallback_model", ""),
            prompt_tokens=_number(self.usage.get("prompt_tokens")),
            completion_tokens=_number(self.usage.get("completion_tokens")),
            cached_tokens=cached,
            cost_usd=self.cost_usd,
        )

    def read_payload(self, payload: bytes) -> None:
        try:
            parsed = json.loads(payload)
        except ValueError:
            return
        if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
            self.usage = parsed["usage"]

    def read_frames(self, lines: list[bytes]) -> None:
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(_DATA_PREFIX) and (payload := stripped[len(_DATA_PREFIX) :].strip()) != _DONE:
                self.read_payload(payload)

    def read_body(self, body: bytes, *, event_stream: bool) -> None:
        if event_stream:
            self.read_frames(body.split(b"\n"))
        else:
            self.read_payload(body)


class _UsageReadingStream(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, request: RequestUsage, *, event_stream: bool) -> None:
        self._inner = inner
        self._request = request
        self._event_stream = event_stream
        self._pending = b""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._pending += chunk
            if self._event_stream:
                *lines, self._pending = self._pending.split(b"\n")
                self._request.read_frames(lines)
            yield chunk
        self._request.read_body(self._pending, event_stream=self._event_stream)
        self._pending = b""

    async def aclose(self) -> None:
        await self._inner.aclose()


class UsageTee:
    """The usage every request of one run reported, in request order."""

    def __init__(self) -> None:
        self.requests: list[RequestUsage] = []

    async def capture(self, response: httpx2.Response) -> None:
        request = RequestUsage(
            resolved_model=response.headers.get(RESOLVED_MODEL_HEADER, ""),
            routing={key: value for key, header in ROUTING_HEADERS.items() if (value := response.headers.get(header))},
        )
        self.requests.append(request)
        event_stream = response.headers.get("content-type", "").startswith(_EVENT_STREAM)
        if response.is_stream_consumed:
            request.read_body(response.content, event_stream=event_stream)
        elif isinstance(response.stream, httpx2.AsyncByteStream):
            response.stream = _UsageReadingStream(response.stream, request, event_stream=event_stream)

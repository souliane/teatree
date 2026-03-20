import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from django.conf import settings
from django.http import HttpRequest, StreamingHttpResponse
from django.views import View

_ALL_PANELS = (
    "summary",
    "tickets",
    "worktrees",
    "headless_queue",
    "queue",
    "sessions",
    "review_comments",
    "activity",
)


class DashboardSSEView(View):
    _poll_interval: float = 2.0
    _heartbeat_every: int = 8  # ticks without data before sending heartbeat

    async def get(self, request: HttpRequest) -> StreamingHttpResponse:  # noqa: ARG002
        response = StreamingHttpResponse(
            streaming_content=self._event_stream(),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    async def _event_stream(self) -> AsyncIterator[bytes]:
        yield _format_sse("connected", {"status": "ok"})
        last_mtime = 0.0
        ticks_since_event = 0
        while True:
            try:
                changed, last_mtime = _detect_changes(last_mtime)
                if changed:
                    ticks_since_event = 0
                    for panel in changed:
                        yield _format_sse(panel, {"panel": panel})
                else:
                    ticks_since_event += 1
                    if ticks_since_event >= self._heartbeat_every:
                        ticks_since_event = 0
                        yield b": heartbeat\n\n"
            except asyncio.CancelledError:
                break
            await asyncio.sleep(self._poll_interval)


def _detect_changes(last_mtime: float) -> tuple[list[str], float]:
    db_path = Path(settings.DATABASES["default"]["NAME"])
    try:
        mtime = db_path.stat().st_mtime
    except FileNotFoundError:
        return [], last_mtime
    if mtime > last_mtime:
        return list(_ALL_PANELS), mtime
    return [], last_mtime


def _format_sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

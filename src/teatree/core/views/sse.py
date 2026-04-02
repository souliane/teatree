import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import HttpRequest, StreamingHttpResponse
from django.views import View

from teatree.core.selectors import (
    build_active_sessions,
    build_automation_summary,
    build_dashboard_summary,
    build_dashboard_ticket_rows,
    build_headless_queue,
    build_interactive_queue,
    build_recent_activity,
    build_review_comments,
    build_worktree_rows,
    invalidate_panel_cache,
)

_PANEL_BUILDERS: dict[str, Callable[[], object]] = {
    "summary": build_dashboard_summary,
    "automation": build_automation_summary,
    "tickets": build_dashboard_ticket_rows,
    "worktrees": build_worktree_rows,
    "headless_queue": build_headless_queue,
    "queue": lambda: build_interactive_queue(pending_only=True),
    "sessions": build_active_sessions,
    "review_comments": build_review_comments,
    "activity": build_recent_activity,
}


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
        last_hashes: dict[str, str] = {}
        ticks_since_event = 0
        while True:
            try:
                detect = sync_to_async(_detect_changed_panels)
                changed_panels, last_mtime, last_hashes = await detect(last_mtime, last_hashes)
                if changed_panels:
                    ticks_since_event = 0
                    for panel in changed_panels:
                        yield _format_sse(panel, {"panel": panel})
                else:
                    ticks_since_event += 1
                    if ticks_since_event >= self._heartbeat_every:
                        ticks_since_event = 0
                        yield b": heartbeat\n\n"
            except asyncio.CancelledError:
                break
            await asyncio.sleep(self._poll_interval)


def _detect_changed_panels(
    last_mtime: float,
    last_hashes: dict[str, str],
) -> tuple[list[str], float, dict[str, str]]:
    """Return only panels whose content actually changed since last check."""
    db_path = Path(settings.DATABASES["default"]["NAME"])
    try:
        mtime = db_path.stat().st_mtime
    except FileNotFoundError:
        return [], last_mtime, last_hashes
    if mtime <= last_mtime:
        return [], last_mtime, last_hashes

    # DB changed — invalidate the panel cache and rebuild
    invalidate_panel_cache()
    changed: list[str] = []
    new_hashes: dict[str, str] = {}
    for panel, builder in _PANEL_BUILDERS.items():
        content_hash = hashlib.md5(repr(builder()).encode(), usedforsecurity=False).hexdigest()
        new_hashes[panel] = content_hash
        if content_hash != last_hashes.get(panel):
            changed.append(panel)

    return changed, mtime, new_hashes


def _format_sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

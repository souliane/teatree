"""What a page did while it was driven: console output, page errors, requests and navigations.

One recorder feeds both consumers — the ``t3 browser`` keeper, which appends every event
to a per-session log so a later step can read what an earlier one caused, and the
pre-push visual-QA gate, which keeps them in memory for one page load.
"""

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import ConsoleMessage, Frame, Page, Request, Response

HTTP_ERROR_MIN = 400


@dataclass(frozen=True, slots=True)
class BrowserEvent:
    kind: str
    text: str = ""
    url: str = ""
    level: str = ""
    method: str = ""
    status: int | None = None
    seq: int = 0

    @property
    def is_finding(self) -> bool:
        match self.kind:
            case "console":
                return self.level == "error"
            case "pageerror" | "requestfailed":
                return True
            case "response":
                return (self.status or 0) >= HTTP_ERROR_MIN
        return False

    def render(self) -> str:
        match self.kind:
            case "console":
                return f"CONSOLE {self.level} {self.text}"
            case "pageerror":
                return f"PAGEERROR {self.text}"
            case "requestfailed":
                return f"FAILED {self.method} {self.url} — {self.text}"
            case "response":
                return f"HTTP {self.status} {self.url}"
        return f"NAVIGATED {self.url}"

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, line: str) -> "BrowserEvent":
        return cls(**json.loads(line))


class EvidenceRecorder:
    def __init__(self, sink: Callable[[BrowserEvent], None]) -> None:
        self._sink = sink

    def attach(self, page: "Page") -> None:
        page.on("console", lambda message: self._console(page, message))
        page.on("pageerror", lambda error: self._sink(BrowserEvent("pageerror", text=str(error), url=page.url)))
        page.on("requestfailed", self._request_failed)
        page.on("response", self._response)
        page.on("framenavigated", lambda frame: self._navigated(page, frame))

    def _console(self, page: "Page", message: "ConsoleMessage") -> None:
        self._sink(BrowserEvent("console", text=message.text, level=message.type, url=page.url))

    def _request_failed(self, request: "Request") -> None:
        self._sink(BrowserEvent("requestfailed", text=request.failure or "", url=request.url, method=request.method))

    def _response(self, response: "Response") -> None:
        self._sink(BrowserEvent("response", url=response.url, status=response.status, method=response.request.method))

    def _navigated(self, page: "Page", frame: "Frame") -> None:
        if frame == page.main_frame:
            self._sink(BrowserEvent("navigation", url=frame.url))


class EventLog:
    """An append-only JSONL log of one session's events, compacted to the newest ``keep``."""

    def __init__(self, path: Path, *, keep: int = 2000) -> None:
        self.path = path
        self._keep = keep
        self._written = 0
        self._seq = self.last_seq()

    def append(self, event: BrowserEvent) -> None:
        self._seq += 1
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(dataclasses.replace(event, seq=self._seq).to_json() + "\n")
        self._written += 1
        if self._written >= self._keep:
            self._compact()

    def read(self, *, after: int = 0) -> list[BrowserEvent]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        events = (BrowserEvent.from_json(line) for line in lines if line.strip())
        return [event for event in events if event.seq > after]

    def last_seq(self) -> int:
        events = self.read()
        return events[-1].seq if events else 0

    def since_last_navigation(self) -> list[BrowserEvent]:
        events = self.read()
        starts = [index for index, event in enumerate(events) if event.kind == "navigation"]
        return events[starts[-1] + 1 :] if starts else events

    def _compact(self) -> None:
        kept = self.read()[-self._keep :]
        staging = self.path.with_suffix(".compacting")
        staging.write_text("".join(event.to_json() + "\n" for event in kept), encoding="utf-8")
        staging.replace(self.path)
        self._written = 0

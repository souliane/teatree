"""The process that owns a session's headless browser between ``t3 browser`` steps.

Started detached by :class:`teatree.browser.session.BrowserSession`, it launches Chromium
with a DevTools endpoint every later step re-attaches to, records what the pages do to
the session's event log, and exits on ``close``, SIGTERM, or an idle session.
"""

import contextlib
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from teatree.browser.evidence import EventLog, EvidenceRecorder
from teatree.browser.state import SessionFiles

IDLE_TIMEOUT_S = 30 * 60
DEVTOOLS_PORT_WAIT_S = 10.0
LAUNCH_PROBE_TIMEOUT_MS = 15_000
_PUMP_MS = 100


def launch_probe() -> str | None:
    """Launch and close one headless browser; the launch error when it cannot, else ``None``."""
    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch(headless=True, timeout=LAUNCH_PROBE_TIMEOUT_MS).close()
    except PlaywrightError as exc:
        return str(exc)
    return None


class Keeper:
    def __init__(self, files: SessionFiles) -> None:
        self.files = files
        self._terminated = False
        self._closed = False

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_sigterm)
        self.files.pid.write_text(str(os.getpid()), encoding="utf-8")
        self.files.last_used.touch()
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(self.files.profile), headless=True, args=["--remote-debugging-port=0"]
                )
                self._serve(context)
        except PlaywrightError as exc:
            self.files.error.write_text(str(exc), encoding="utf-8")
            return 1
        finally:
            self.files.endpoint.unlink(missing_ok=True)
        return 0

    def _serve(self, context: BrowserContext) -> None:
        context.on("close", lambda _context: self._mark_closed())
        recorder = EvidenceRecorder(EventLog(self.files.events).append)
        context.on("page", recorder.attach)
        for page in context.pages:
            recorder.attach(page)
        if not context.pages:
            context.new_page()
        self._publish_endpoint(self._devtools_url())
        while not self._should_stop():
            self._pump(context)
        with contextlib.suppress(PlaywrightError):
            context.close()

    def _devtools_url(self) -> str:
        port_file = self.files.profile / "DevToolsActivePort"
        deadline = time.monotonic() + DEVTOOLS_PORT_WAIT_S
        while not port_file.is_file():
            if time.monotonic() > deadline:
                msg = f"Chromium wrote no {port_file.name} within {DEVTOOLS_PORT_WAIT_S:.0f}s"
                raise PlaywrightError(msg)
            time.sleep(0.05)
        return f"http://127.0.0.1:{port_file.read_text(encoding='utf-8').splitlines()[0]}"

    def _publish_endpoint(self, cdp_url: str) -> None:
        staging = self.files.endpoint.with_suffix(".staging")
        staging.write_text(
            json.dumps({"pid": os.getpid(), "cdp_url": cdp_url, "started_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )
        staging.replace(self.files.endpoint)

    @staticmethod
    def _pump(context: BrowserContext) -> None:
        pages: list[Page] = context.pages
        with contextlib.suppress(PlaywrightError):
            (pages[0] if pages else context.new_page()).wait_for_timeout(_PUMP_MS)

    def _should_stop(self) -> bool:
        if self._terminated or self._closed or self.files.stop.exists():
            return True
        try:
            idle = time.time() - self.files.last_used.stat().st_mtime
        except FileNotFoundError:
            return True
        return idle > IDLE_TIMEOUT_S

    def _mark_closed(self) -> None:
        self._closed = True

    def _on_sigterm(self, _signum: int, _frame: FrameType | None) -> None:
        self._terminated = True


def main(state_dir: Path) -> int:
    # Fork so the keeper outlives the `t3 browser` step that launched it, unparented.
    if os.fork():
        return 0
    os.setsid()
    return Keeper(SessionFiles(state_dir)).run()


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))

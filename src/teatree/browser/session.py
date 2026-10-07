"""A headless browser held open per worktree, which each ``t3 browser`` step re-attaches to.

The browser lives in a detached keeper process (:mod:`teatree.browser.keeper`) exposing a
DevTools endpoint; a step connects over CDP, acts, waits for the page to go quiet, and
reads back what the keeper recorded — so console output and failed requests caused by an
earlier step are still there for ``inspect``.

Whether a keeper is alive is read from the kernel lock it holds on ``keeper.pid``, never
from the pid recorded there: a recorded pid outlives its process and can come to name an
unrelated one, so it is only ever signalled while that lock is held.
"""

import contextlib
import fcntl
import json
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from teatree.browser.evidence import BrowserEvent, EventLog
from teatree.browser.state import KEEPER_LOCK, SessionFiles
from teatree.core.loop_lease_liveness import namespace_is_proven
from teatree.paths import PathHelpers
from teatree.utils.run import spawn_session_leader
from teatree.utils.singleton import flock_is_held, pid_alive, read_holder
from teatree.utils.work_tree import WorkTreeError, resolve

if TYPE_CHECKING:
    from playwright.sync_api import Page

KEEPER_START_TIMEOUT_S = 20.0
KEEPER_STOP_TIMEOUT_S = 10.0
KEEPER_KILL_WAIT_S = 2.0
NAVIGATION_TIMEOUT_S = 30.0
SETTLE_QUIET_S = 0.5
SETTLE_MAX_S = 5.0
ACTION_TIMEOUT_MS = 10_000
REMEDY = (
    "Install Playwright's headless Chromium with `t3 doctor check --repair` "
    "(or `python -m playwright install chromium-headless-shell`)."
)


class BrowserError(RuntimeError):
    pass


class BrowserUnavailableError(BrowserError):
    pass


class NoBrowserSessionError(BrowserError):
    pass


class StepFailedError(BrowserError):
    def __init__(self, message: str, events: list[BrowserEvent] | None = None) -> None:
        super().__init__(message)
        self.events = events or []


class Verb(StrEnum):
    CLICK = "click"
    FILL = "fill"
    TYPE = "type"
    PRESS = "press"
    UPLOAD = "upload"
    WAIT = "wait"
    EVAL = "eval"


_ARGUMENTS: dict[Verb, tuple[str, ...]] = {
    Verb.CLICK: ("SELECTOR",),
    Verb.FILL: ("SELECTOR", "VALUE"),
    Verb.TYPE: ("SELECTOR", "TEXT"),
    Verb.PRESS: ("SELECTOR", "KEY"),
    Verb.UPLOAD: ("SELECTOR", "FILE..."),
    Verb.WAIT: ("SELECTOR",),
    Verb.EVAL: ("EXPRESSION",),
}


@dataclass(frozen=True, slots=True)
class StepReport:
    url: str
    title: str
    events: list[BrowserEvent]
    result: object = None


@dataclass(frozen=True, slots=True)
class Inspection:
    url: str
    title: str
    aria_snapshot: str
    snapshot_path: Path
    html_path: Path
    screenshot_path: Path
    events: list[BrowserEvent]


class BrowserSession:
    def __init__(self, files: SessionFiles) -> None:
        self.files = files

    @classmethod
    def for_checkout(cls, checkout: Path) -> "BrowserSession":
        return cls(SessionFiles(PathHelpers.browser_session_dir(checkout)))

    @classmethod
    def for_directory(cls, directory: Path) -> "BrowserSession":
        try:
            checkout = resolve(directory).root
        except WorkTreeError:
            checkout = directory.resolve()
        return cls.for_checkout(checkout)

    def cdp_url(self) -> str | None:
        """The live session's DevTools endpoint, or ``None`` when no keeper answers on it."""
        if not self._keeper_running():
            return None
        try:
            cdp_url = str(json.loads(self.files.endpoint.read_text(encoding="utf-8"))["cdp_url"])
            httpx.get(f"{cdp_url}/json/version", timeout=2.0, trust_env=False).raise_for_status()
        except (OSError, ValueError, KeyError, httpx.HTTPError):
            return None
        return cdp_url

    def open(self, url: str, *, timeout_s: float = NAVIGATION_TIMEOUT_S) -> StepReport:
        return self._step(self._ensure_keeper(), lambda page: _goto(page, url, timeout_s))

    def act(self, verb: Verb, arguments: list[str]) -> StepReport:
        expected = _ARGUMENTS[verb]
        variadic = expected[-1].endswith("...")
        if len(arguments) < len(expected) or (not variadic and len(arguments) > len(expected)):
            msg = f"`act {verb}` takes {' '.join(expected)}; got {len(arguments)} argument(s)"
            raise StepFailedError(msg)
        return self._step(self._require_session(), lambda page: _perform(page, verb, arguments))

    def inspect(self) -> Inspection:
        cdp_url = self._require_session()
        snapshot, html, screenshot = (self.files.artifacts / name for name in ("aria.yaml", "page.html", "page.png"))
        with self._attach(cdp_url) as page:
            self.files.artifacts.mkdir(exist_ok=True)
            aria = page.locator("body").aria_snapshot()
            snapshot.write_text(aria, encoding="utf-8")
            html.write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(screenshot))
            return Inspection(
                url=page.url,
                title=page.title(),
                aria_snapshot=aria,
                snapshot_path=snapshot,
                html_path=html,
                screenshot_path=screenshot,
                events=EventLog(self.files.events).since_last_navigation(),
            )

    def close(self) -> bool:
        if not self.files.root.is_dir():
            return False
        self.files.stop.touch()
        self._stop_keeper(grace_s=KEEPER_STOP_TIMEOUT_S)
        shutil.rmtree(self.files.root, ignore_errors=True)
        return True

    def _step(self, cdp_url: str, action: Callable[["Page"], object]) -> StepReport:
        log = EventLog(self.files.events)
        before = log.last_seq()
        try:
            with self._attach(cdp_url) as page:
                result = action(page)
                _settle(log)
                return StepReport(url=page.url, title=page.title(), events=log.read(after=before), result=result)
        except StepFailedError as exc:
            raise StepFailedError(str(exc), log.read(after=before)) from exc

    @contextlib.contextmanager
    def _attach(self, cdp_url: str) -> Iterator["Page"]:
        from playwright.sync_api import (  # noqa: PLC0415 — deferred: keeps Playwright off `t3` startup
            Error,
            sync_playwright,
        )

        try:
            with _owner_only(), sync_playwright() as playwright:
                self.files.last_used.touch()
                context = playwright.chromium.connect_over_cdp(cdp_url).contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(ACTION_TIMEOUT_MS)
                yield page
        except Error as exc:
            raise StepFailedError(str(exc)) from exc

    def _require_session(self) -> str:
        cdp_url = self.cdp_url()
        if cdp_url is None:
            msg = "no open browser session for this worktree — start one with `t3 browser open <url>`"
            raise NoBrowserSessionError(msg)
        return cdp_url

    def _ensure_keeper(self) -> str:
        with _owner_only():
            self.files.root.mkdir(parents=True, exist_ok=True)
            self.files.root.chmod(0o700)
            with self.files.lock.open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                return self.cdp_url() or self._start_keeper()

    def _start_keeper(self) -> str:
        self._stop_keeper(grace_s=0)
        for stale in (self.files.endpoint, self.files.error, self.files.stop):
            stale.unlink(missing_ok=True)
        shutil.rmtree(self.files.profile, ignore_errors=True)
        with self.files.log.open("ab") as log:
            launcher = [sys.executable, "-m", "teatree.browser.keeper", str(self.files.root)]
            exit_code = spawn_session_leader(launcher, stdout=log, stderr=log).wait()
        if exit_code != 0:
            msg = f"the browser keeper could not start (exit {exit_code}); see {self.files.log}"
            raise BrowserUnavailableError(msg)
        deadline = time.monotonic() + KEEPER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            # The keeper publishes its endpoint only once it holds its lock, so probing waits for the file.
            if self.files.endpoint.is_file() and (cdp_url := self.cdp_url()) is not None:
                return cdp_url
            if self.files.error.is_file():
                msg = f"the headless browser did not launch: {self.files.error.read_text(encoding='utf-8')}\n{REMEDY}"
                raise BrowserUnavailableError(msg)
            time.sleep(0.1)
        self._stop_keeper(grace_s=0)
        msg = f"no browser endpoint within {KEEPER_START_TIMEOUT_S:.0f}s; see {self.files.log}"
        raise BrowserUnavailableError(msg)

    def _stop_keeper(self, *, grace_s: float) -> None:
        """Wait for the keeper to release its lock; past *grace_s*, signal the lock's own recorded holder."""
        keeper = self._local_keeper_pid()
        if not self._wait_released(grace_s):
            self._terminate(self._local_keeper_pid())
        if keeper is not None:
            _wait_exited(keeper, KEEPER_KILL_WAIT_S)

    def _terminate(self, keeper: int | None) -> None:
        if keeper is None:
            msg = (
                f"a browser keeper still holds {self.files.pid}, but its pid cannot be resolved from this "
                "runtime (it runs in another container or host) — close the session from where it was opened"
            )
            raise BrowserUnavailableError(msg)
        for signum in (signal.SIGTERM, signal.SIGKILL):
            if self._keeper_running():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(keeper, signum)
            if self._wait_released(KEEPER_KILL_WAIT_S):
                return

    def _local_keeper_pid(self) -> int | None:
        """The pid recorded by the keeper holding the lock, when this runtime shares its pid namespace."""
        if not self._keeper_running():
            return None
        holder = read_holder(self.files.pid)
        if holder is None or not namespace_is_proven(holder.context.pid_namespace):
            return None
        return holder.pid

    def _keeper_running(self) -> bool:
        return self.files.pid.is_file() and flock_is_held(KEEPER_LOCK, pid_path=self.files.pid)

    def _wait_released(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while self._keeper_running():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True


def _wait_exited(pid: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)


@contextlib.contextmanager
def _owner_only() -> Iterator[None]:
    """Create every file and directory inside the block readable by its owner alone."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _goto(page: "Page", url: str, timeout_s: float) -> None:
    page.goto(url, wait_until="load", timeout=timeout_s * 1000)


def _perform(page: "Page", verb: Verb, arguments: list[str]) -> object:
    if verb is Verb.EVAL:
        return page.evaluate(arguments[0])
    target = page.locator(arguments[0])
    match verb:
        case Verb.CLICK:
            target.click()
        case Verb.FILL:
            target.fill(arguments[1])
        case Verb.TYPE:
            target.press_sequentially(arguments[1])
        case Verb.PRESS:
            target.press(arguments[1])
        case Verb.UPLOAD:
            target.set_input_files(arguments[1:])
        case Verb.WAIT:
            target.wait_for()
    return None


def _settle(log: EventLog) -> None:
    """Return once the keeper has logged nothing new for ``SETTLE_QUIET_S`` (bounded)."""
    deadline = time.monotonic() + SETTLE_MAX_S
    last, quiet_since = log.last_seq(), time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        if (seq := log.last_seq()) != last:
            last, quiet_since = seq, time.monotonic()
        elif time.monotonic() - quiet_since >= SETTLE_QUIET_S:
            return

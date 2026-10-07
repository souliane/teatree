"""Page objects for the ``t3 browser`` CLI: each claim raises ``AssertionError`` naming the evidence it read."""

import json
import os
import shutil
import signal
import stat
import time
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, NoReturn

import httpx
from typer.testing import CliRunner, Result

from teatree.browser.keeper import IDLE_TIMEOUT_S, launch_probe
from teatree.browser.session import BrowserSession
from teatree.cli.browser import browser_app
from teatree.utils.singleton import read_pid

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _fail(message: str) -> NoReturn:
    raise AssertionError(message)


def _is_finding(event: dict[str, Any]) -> bool:
    match event["kind"]:
        case "console":
            return event["level"] == "error"
        case "pageerror" | "requestfailed":
            return True
        case "response":
            return event["status"] >= HTTPStatus.BAD_REQUEST
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def require_findings(found: set[tuple[str, str]], *expected: tuple[str, str]) -> None:
    if missing := set(expected) - found:
        _fail(f"findings {sorted(missing)} missing from {sorted(found)}")


def require_launchable_browser() -> None:
    if (failure := launch_probe()) is not None:
        _fail(failure.reason)


@dataclass(frozen=True, slots=True)
class StepReport:
    payload: dict[str, Any]

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.payload["events"]

    def require_event(self, **fields: object) -> None:
        if not any(all(event.get(key) == value for key, value in fields.items()) for event in self.events):
            _fail(f"no event matching {fields} in {self.events}")

    def require_no_event(self, **fields: object) -> None:
        if matching := [event for event in self.events if all(event.get(k) == v for k, v in fields.items())]:
            _fail(f"events of an earlier page in the report: {matching}")

    def require_no_findings(self) -> None:
        if findings := [event for event in self.events if _is_finding(event)]:
            _fail(f"findings on a clean page: {findings}")

    def require_console(self, *texts: str) -> None:
        seen = {event["text"] for event in self.events if event["kind"] == "console"}
        if missing := set(texts) - seen:
            _fail(f"console messages {sorted(missing)} missing from {sorted(seen)}")

    def require_snapshot_line(self, line: str) -> None:
        if line not in self.payload["aria_snapshot"]:
            _fail(f"{line!r} not in the snapshot:\n{self.payload['aria_snapshot']}")

    def require_png_screenshot(self) -> None:
        if Path(self.payload["screenshot"]).read_bytes()[:8] != _PNG_MAGIC:
            _fail(f"{self.payload['screenshot']} is not a PNG")


class BrowserCli:
    def __init__(self, checkout: Path) -> None:
        self.session = BrowserSession.for_checkout(checkout)

    def step(self, *arguments: str) -> StepReport:
        result = self._invoke(*arguments, "--json")
        if result.exit_code != 0:
            _fail(f"`t3 browser {' '.join(arguments)}` exited {result.exit_code}:\n{result.output}")
        return StepReport(json.loads(result.stdout))

    def failed_step(self, *arguments: str, naming: str) -> StepReport:
        """The JSON a step that exits 1 still prints: its error and what the page did up to it."""
        result = self._invoke(*arguments, "--json")
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
        if result.exit_code != 1 or naming not in payload.get("error", ""):
            _fail(f"expected a failed step naming {naming!r}, got {result.exit_code}:\n{result.output}")
        return StepReport(payload)

    def refused(self, *arguments: str, naming: str) -> None:
        result = self._invoke(*arguments)
        if result.exit_code != 1 or naming not in result.output:
            _fail(f"expected a refusal naming {naming!r}, got {result.exit_code}:\n{result.output}")

    def close(self) -> None:
        if (result := self._invoke("close")).exit_code != 0:
            _fail(result.output)

    def keeper_pid(self) -> int:
        pid = read_pid(self.session.files.pid)
        if pid is None:
            _fail(f"no live keeper is recorded in {self.session.files.pid}")
        return pid

    def require_one_headless_keeper(self, pid: int) -> None:
        if self.keeper_pid() != pid:
            _fail(f"the keeper changed from {pid} to {self.keeper_pid()}: a step relaunched it")
        if (cdp_url := self.session.cdp_url()) is None:
            _fail("the session has no live DevTools endpoint")
        browser = httpx.get(f"{cdp_url}/json/version", trust_env=False).json()["Browser"]
        if "HeadlessChrome" not in browser:
            _fail(f"the session browser is not headless: {browser}")

    @staticmethod
    def require_keeper_gone(pid: int, *, within_s: float = 0.0) -> None:
        deadline = time.monotonic() + within_s
        while _alive(pid):
            if time.monotonic() >= deadline:
                _fail(f"keeper {pid} is still running")
            time.sleep(0.1)

    def require_owner_only_session_files(self) -> None:
        root = self.session.files.root
        modes = {path.name: stat.S_IMODE(path.stat().st_mode) for path in (root, *root.iterdir())}
        if exposed := {name: oct(mode) for name, mode in modes.items() if mode & 0o077}:
            _fail(f"session state readable beyond its owner: {exposed}")

    def require_a_keeper_other_than(self, exited: int) -> None:
        if self.keeper_pid() == exited:
            _fail(f"keeper {exited} is recorded as live after it exited")

    def end_keeper_with_sigterm(self) -> int:
        pid = self.keeper_pid()
        os.kill(pid, signal.SIGTERM)
        return pid

    def let_the_session_go_idle(self) -> int:
        pid = self.keeper_pid()
        long_ago = time.time() - IDLE_TIMEOUT_S - 1
        os.utime(self.session.files.last_used, (long_ago, long_ago))
        return pid

    def remove_session_directory(self) -> int:
        pid = self.keeper_pid()
        shutil.rmtree(self.session.files.root)
        return pid

    def require_no_session_directory(self) -> None:
        if self.session.files.root.exists():
            _fail(f"{self.session.files.root} still holds {sorted(self.session.files.root.rglob('*'))[:10]}")

    @staticmethod
    def _invoke(*arguments: str) -> Result:
        return CliRunner().invoke(browser_app, list(arguments))

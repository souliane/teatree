"""Page objects for the ``t3 browser`` CLI: each claim raises ``AssertionError`` naming the evidence it read."""

import json
import os
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, NoReturn

import httpx
from typer.testing import CliRunner, Result

from teatree.browser.keeper import launch_probe
from teatree.browser.session import BrowserSession
from teatree.cli.browser import browser_app

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


def require_launchable_browser() -> None:
    if (failure := launch_probe()) is not None:
        _fail(failure)


@dataclass(frozen=True, slots=True)
class StepReport:
    payload: dict[str, Any]

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.payload["events"]

    def require_event(self, **fields: object) -> None:
        if not any(all(event.get(key) == value for key, value in fields.items()) for event in self.events):
            _fail(f"no event matching {fields} in {self.events}")

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

    def refused(self, *arguments: str, naming: str) -> None:
        result = self._invoke(*arguments)
        if result.exit_code != 1 or naming not in result.output:
            _fail(f"expected a refusal naming {naming!r}, got {result.exit_code}:\n{result.output}")

    def close(self) -> None:
        if (result := self._invoke("close")).exit_code != 0:
            _fail(result.output)

    def keeper_pid(self) -> int:
        return int(self.session.files.pid.read_text(encoding="utf-8"))

    def require_one_headless_keeper(self, pid: int) -> None:
        if self.keeper_pid() != pid:
            _fail(f"the keeper changed from {pid} to {self.keeper_pid()}: a step relaunched it")
        if (cdp_url := self.session.cdp_url()) is None:
            _fail("the session has no live DevTools endpoint")
        browser = httpx.get(f"{cdp_url}/json/version", trust_env=False).json()["Browser"]
        if "HeadlessChrome" not in browser:
            _fail(f"the session browser is not headless: {browser}")

    @staticmethod
    def require_keeper_gone(pid: int) -> None:
        if _alive(pid):
            _fail(f"keeper {pid} survived close")

    @staticmethod
    def _invoke(*arguments: str) -> Result:
        return CliRunner().invoke(browser_app, list(arguments))

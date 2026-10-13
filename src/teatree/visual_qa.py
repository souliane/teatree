"""Pre-push browser sanity gate for frontend PRs.

Loads the page(s) the diff actually touches in a real browser and reports
silent-render regressions: page crashes, console errors, raw ``app.*``
translation keys, blocking asset 404s.

Designed as a bounded pre-push check, not a regression suite. The checkout
server gets up to 90 seconds to start and the browser up to 60 seconds to
inspect its pages. An unavailable server or browser is a did-not-run verdict;
only an explicit, recorded skip bypasses it.

The gate is a precondition of PR creation: ``pr create`` calls
``_run_visual_qa_gate`` before composing the PR, persists the summary on
``Ticket.extra['visual_qa']`` so the result survives in the FSM history,
and refuses to create the PR when findings are present or the check did not run.
"""

import contextlib
import fnmatch
import os
import re
import signal
import socket
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.browser.evidence import BrowserEvent, EvidenceRecorder
from teatree.core.overlay import OverlayBase
from teatree.types import RunCommand
from teatree.utils import git
from teatree.utils.run import CommandFailedError, Popen, TimeoutExpired, redact_secrets, spawn_session_leader

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

    from teatree.core.models.types import VisualQASummary

# Default file patterns that warrant a browser sanity check.
# Overlays can override via ``OverlayBase.review.visual_qa_targets()``.
DEFAULT_TRIGGER_GLOBS: tuple[str, ...] = (
    "*.html",
    "*.scss",
    "*.css",
    "*.component.ts",
    "*.module.ts",
    "*.routes.ts",
    "*.routing.ts",
    "**/i18n/*.json",
    "**/locale/*.po",
    "**/templates/**",
    "**/static/**",
)

MAX_PAGES = 12
PER_PAGE_TIMEOUT_MS = 10_000
TOTAL_TIMEOUT_S = 60
DEFAULT_SCREENSHOT_DIR = ".t3/visual_qa"
SERVER_START_TIMEOUT_S = 90

# 401/403 are common when an authenticated-only flow is logged out — not a blocker.
_HTTP_ERROR_THRESHOLD = 400
_NON_BLOCKING_STATUSES = frozenset({401, 403})

_TRANSLATION_KEY_RE = re.compile(r"\bapp\.[a-z][a-z0-9_]*(?:\.[a-z0-9_]+){1,}\b", re.IGNORECASE)


class VisualQAUnavailableError(RuntimeError):
    """Raised when Playwright cannot run — the check did not run, so it cannot pass."""


@dataclass(slots=True)
class ServedApp:
    base_url: str
    process: Popen[str]
    pgid: int
    log_path: Path

    def failure(self) -> str:
        code = self.process.poll()
        if code is None:
            return ""
        tail = redact_secrets("\n".join(self.log_path.read_text(errors="replace").splitlines()[-12:]))
        return f"server exited {code}: {tail}" if tail else f"server exited {code}"

    def wait_until_ready(self, port: int) -> None:
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if failure := self.failure():
                raise VisualQAUnavailableError(failure)
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.2)
        message = f"checkout server did not listen within {SERVER_START_TIMEOUT_S}s"
        raise VisualQAUnavailableError(message)

    def stop(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pgid, signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.pgid, signal.SIGKILL)
            self.process.wait(timeout=5)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pgid, signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class ServerSpec:
    command: RunCommand
    repo_path: str
    prepare: Callable[[], None]


@dataclass(frozen=True, slots=True)
class PageError:
    url: str
    kind: str  # "page", "console", "translation", "http"
    message: str


@dataclass(frozen=True, slots=True)
class PageResult:
    url: str
    screenshot_path: str = ""
    errors: list[PageError] = field(default_factory=list)
    unreached: bool = False


@dataclass(frozen=True, slots=True)
class VisualQAReport:
    targets: list[str]
    pages: list[PageResult] = field(default_factory=list)
    skipped_reason: str = ""
    base_url: str = ""
    not_run_reason: str = ""
    server_failure: str = ""

    @property
    def unchecked(self) -> list[str]:
        return list(self.unchecked_reasons)

    @property
    def unchecked_reasons(self) -> dict[str, str]:
        reasons = {
            target: f"unreached{f' ({self.server_failure})' if self.server_failure else ''}"
            for target, page in zip(self.targets, self.pages, strict=False)
            if page.unreached
        }
        reasons.update(
            {
                target: (
                    "page cap"
                    if index >= MAX_PAGES
                    else f"unreached ({self.server_failure or self.not_run_reason})"
                    if self.server_failure or self.not_run_reason
                    else "deadline"
                )
                for index, target in enumerate(self.targets[len(self.pages) :], start=len(self.pages))
            }
        )
        return reasons

    @property
    def has_errors(self) -> bool:
        return bool(self.unchecked) or any(page.errors for page in self.pages)

    @property
    def total_errors(self) -> int:
        return sum(len(page.errors) for page in self.pages)

    @property
    def pages_checked(self) -> int:
        return sum(not page.unreached for page in self.pages)

    def summary(self) -> "VisualQASummary":
        """Return a JSON-serialisable snapshot for ``Ticket.extra``."""
        from teatree.core.models.types import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
            VisualQAPageDetail,
            VisualQAPageError,
            VisualQASummary,
        )

        details: list[VisualQAPageDetail] = [
            {
                "url": page.url,
                "errors": [VisualQAPageError(kind=e.kind, message=e.message) for e in page.errors],
            }
            for page in self.pages
        ]
        return VisualQASummary(
            targets=list(self.targets),
            skipped_reason=self.skipped_reason,
            not_run_reason=self.not_run_reason,
            base_url=self.base_url,
            pages_checked=self.pages_checked,
            errors=self.total_errors,
            details=details,
            unchecked_reasons=self.unchecked_reasons,
            report_markdown=format_report(self),
        )


# ── Detection ────────────────────────────────────────────────────────


def changed_files(repo: str = ".", base: str = "") -> list[str]:
    """Return paths changed on the current branch vs *base* (default: the resolved diff base).

    Raises ``CommandFailedError`` when git cannot diff, so a failed read never reads as "no changes".
    """
    out = git.run_strict(repo=repo, args=["diff", "--name-only", f"{base or git.resolve_diff_base(repo)}...HEAD"])
    return [line for line in out.splitlines() if line]


def matches_triggers(paths: list[str], globs: tuple[str, ...] = DEFAULT_TRIGGER_GLOBS) -> list[str]:
    """Return the subset of *paths* matching any glob in *globs*."""
    return [path for path in paths if any(fnmatch.fnmatch(path, glob) for glob in globs)]


def detect_targets(diff: list[str], overlay: OverlayBase | None = None) -> list[str]:
    """Return URL paths to load given the changed files.

    When *overlay* exposes ``review.visual_qa_targets``, defer to it so each
    project can map diff paths to the URLs it cares about.  Otherwise fall
    back to the default trigger globs and return ``["/"]`` if any matched.
    """
    if overlay is not None:
        targets = overlay.review.visual_qa_targets(diff)
        return list(targets) if targets else []
    return ["/"] if matches_triggers(diff) else []


# ── Runner ───────────────────────────────────────────────────────────


def run_check(targets: list[str], base_url: str, screenshot_dir: str = DEFAULT_SCREENSHOT_DIR) -> list[PageResult]:
    """Load each target URL and capture errors + a single screenshot.

    Returns one ``PageResult`` per target.  Raises
    ``VisualQAUnavailableError`` when the headless browser cannot start.
    """
    try:
        from playwright.sync_api import (  # noqa: PLC0415 — deferred: ~0.5 s on every overlay load
            Error as PlaywrightError,
        )
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 — deferred: ~0.5 s on every overlay load
    except ImportError as exc:
        msg = (
            f"Playwright is not importable in the environment running `t3` ({exc}). "
            "Repair it with `t3 doctor check --repair`."
        )
        raise VisualQAUnavailableError(msg) from exc

    out_dir = Path(screenshot_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + TOTAL_TIMEOUT_S
    results: list[PageResult] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            for index, target in enumerate(targets[:MAX_PAGES]):
                if time.monotonic() >= deadline:
                    break
                results.append(_check_one(context, base_url, target, out_dir, index))
            context.close()
            browser.close()
    except PlaywrightError as exc:
        msg = (
            f"the headless browser failed to launch ({exc.__class__.__name__}): {exc}. "
            "Repair it with `t3 doctor check --repair`."
        )
        raise VisualQAUnavailableError(msg) from exc

    return results


def _check_one(context: "BrowserContext", base_url: str, target: str, out_dir: Path, index: int) -> PageResult:
    from playwright.sync_api import Error as PlaywrightError  # noqa: PLC0415 — deferred: ~0.5 s on every overlay load

    url = base_url.rstrip("/") + "/" + target.lstrip("/")
    events: list[BrowserEvent] = []
    page = context.new_page()
    EvidenceRecorder(events.append).attach(page)

    navigation_error = ""
    unreached = False
    try:
        page.goto(url, timeout=PER_PAGE_TIMEOUT_MS, wait_until="networkidle")
    except PlaywrightError as exc:
        unreached = _never_connected(str(exc))
        if not unreached:
            navigation_error = f"navigation failed: {exc}"

    body_text = ""
    with contextlib.suppress(Exception):
        body_text = page.locator("body").inner_text(timeout=2_000)

    screenshot_path = ""
    slug = _slug(target, index)
    candidate = out_dir / f"{slug}.png"
    try:
        page.screenshot(path=str(candidate), full_page=False, animations="disabled")
        screenshot_path = str(candidate)
    except PlaywrightError:
        screenshot_path = ""

    page.close()
    errors = [] if unreached else [error for event in events if (error := _page_error(url, event)) is not None]
    if navigation_error:
        errors.append(PageError(url=url, kind="page", message=navigation_error))
    errors.extend(
        PageError(url=url, kind="translation", message=f"raw key in DOM: {match}")
        for match in _TRANSLATION_KEY_RE.findall(body_text)
    )
    return PageResult(url=url, screenshot_path=screenshot_path, errors=errors, unreached=unreached)


def _never_connected(message: str) -> bool:
    return any(
        code in message
        for code in (
            "net::ERR_CONNECTION_REFUSED",
            "net::ERR_NAME_NOT_RESOLVED",
            "net::ERR_ADDRESS_UNREACHABLE",
            "net::ERR_NETWORK_UNREACHABLE",
        )
    )


@contextlib.contextmanager
def _served_app(server: ServerSpec) -> Iterator[ServedApp]:
    with tempfile.TemporaryDirectory(prefix="teatree-visual-qa-") as scratch:
        server.prepare()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = dict(os.environ)
        for key in ("T3_CONFIG_DB", "T3_CONTROL_DB_DIR", "T3_DATA_DIR", "DATABASE_URL", "DJANGO_SETTINGS_MODULE"):
            env.pop(key, None)
        env.update(server.command.env)
        env.update({"XDG_DATA_HOME": scratch, "TMPDIR": scratch, "T3_VISUAL_QA_PORT": str(port)})
        log_path = Path(scratch) / "server.log"
        with log_path.open("w+") as log:
            try:
                process = spawn_session_leader(
                    server.command.args,
                    cwd=server.command.cwd or server.repo_path,
                    env=env,
                    streams=(log, log),
                )
            except OSError as exc:
                message = f"checkout server could not start: {exc}"
                raise VisualQAUnavailableError(message) from exc
            served = ServedApp(f"http://127.0.0.1:{port}", process, process.pid, log_path)
            try:
                served.wait_until_ready(port)
                yield served
            finally:
                served.stop()


def _page_error(url: str, event: BrowserEvent) -> PageError | None:
    match event.kind:
        case "pageerror":
            return PageError(url=url, kind="page", message=event.text)
        case "console" if event.level == "error":
            return PageError(url=url, kind="console", message=event.text)
        case "response" if (status := event.status or 0) >= _HTTP_ERROR_THRESHOLD and (
            status not in _NON_BLOCKING_STATUSES
        ):
            return PageError(url=url, kind="http", message=f"HTTP {status}: {event.url}")
    return None


def _slug(target: str, index: int) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")
    return f"{index:02d}-{cleaned or 'root'}"


# ── Orchestration ────────────────────────────────────────────────────


def evaluate(
    *,
    read_diff: Callable[[], list[str]],
    overlay: OverlayBase | None,
    base_url: str | Callable[[], str],
    skip_reason: str = "",
    server: ServerSpec | None = None,
) -> VisualQAReport:
    """Run the full gate end to end and return the report.

    Single entry point used by the shipping gate. An explicit bypass still
    gathers evidence; no frontend change returns a clean report.
    """
    try:
        targets = detect_targets(read_diff(), overlay)
    except CommandFailedError as exc:
        return VisualQAReport(
            targets=[],
            not_run_reason=str(exc),
            skipped_reason=f"--skip: {skip_reason}" if skip_reason else "",
        )
    if not targets:
        return VisualQAReport(targets=[], skipped_reason="no frontend changes")

    resolved_url = ""
    server_failure = ""
    try:
        app_context: contextlib.AbstractContextManager[ServedApp | None] = (
            _served_app(server) if server is not None else contextlib.nullcontext(None)
        )
        with app_context as served:
            if served is not None:
                resolved_url = served.base_url
            elif isinstance(base_url, str):
                resolved_url = base_url
            else:
                resolved_url = base_url()
            pages = run_check(targets, resolved_url)
            if served is not None:
                server_failure = served.failure()
    except (VisualQAUnavailableError, RuntimeError) as exc:
        return VisualQAReport(
            targets=targets,
            base_url=resolved_url,
            not_run_reason=str(exc),
            server_failure=str(exc) if server is not None and not resolved_url else "",
            skipped_reason=f"--skip: {skip_reason}" if skip_reason else "",
        )

    return VisualQAReport(
        targets=targets,
        pages=pages,
        base_url=resolved_url,
        server_failure=server_failure,
        skipped_reason=f"--skip: {skip_reason}" if skip_reason else "",
    )


# ── Report ───────────────────────────────────────────────────────────


def _report_header(report: VisualQAReport) -> list[str]:
    lines = ["## Visual QA", ""]
    if report.skipped_reason == "no frontend changes":
        lines.append(f"_skipped: {report.skipped_reason}_")
    elif report.skipped_reason:
        lines.extend((f"_explicit bypass: {report.skipped_reason}_", ""))
    if report.not_run_reason:
        lines.append(f"_did not run: {report.not_run_reason}_")
    if not report.targets and not report.skipped_reason and not report.not_run_reason:
        lines.append("_no frontend changes detected_")
    return lines


def format_report(report: VisualQAReport) -> str:
    """Render a ``## Visual QA`` markdown section for the PR description."""
    lines = _report_header(report)
    if not report.targets:
        return "\n".join(lines) + "\n"

    base = report.base_url
    if base:
        lines.extend((f"_base url: {base}_", ""))
    lines.extend(
        (
            f"Checked {report.pages_checked} of {len(report.targets)} page(s) — {report.total_errors} finding(s).",
            "",
        ),
    )
    if report.unchecked:
        lines.append("**Not checked**")
        lines.extend(f"- `{path}`: {reason}" for path, reason in report.unchecked_reasons.items())
        lines.append("")
    for page in report.pages:
        path = page.url.removeprefix(base) or "/" if base else page.url
        marker = ":x:" if page.errors else ":warning:" if page.unreached else ":white_check_mark:"
        lines.append(f"### {marker} `{path}`")
        if page.screenshot_path:
            lines.append(f"![{path}]({page.screenshot_path})")
        if page.errors:
            lines.append("")
            lines.extend(f"- **{error.kind}**: {error.message}" for error in page.errors)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

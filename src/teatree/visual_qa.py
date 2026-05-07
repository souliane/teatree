"""Pre-push browser sanity gate for frontend PRs.

Loads the page(s) the diff actually touches in a real browser and reports
silent-render regressions: page crashes, console errors, raw ``app.*``
translation keys, blocking asset 404s.

Designed as a fast pre-push gate, not a regression suite.  Hard caps keep
the gate well under 60 seconds per PR.  When Playwright is unavailable
the gate skips with a clear message instead of blocking the push.

The gate is a precondition of PR creation: ``pr create`` calls
``_run_visual_qa_gate`` before composing the PR, persists the summary on
``Ticket.extra['visual_qa']`` so the result survives in the FSM history,
and refuses to create the PR when findings are present.
"""

import contextlib
import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from teatree.core.models.types import VisualQAPageDetail, VisualQAPageError, VisualQASummary
from teatree.core.overlay import OverlayBase
from teatree.utils import git

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

# Playwright is imported lazily so ``pip install teatree`` does not require it.
# When it is absent, ``run_check`` raises ``VisualQAUnavailableError`` and the
# gate fails open with a clear message instead of blocking the push.
PlaywrightError: type[BaseException] = Exception
sync_playwright: Any = None
try:
    from playwright.sync_api import Error as _PlaywrightError
    from playwright.sync_api import sync_playwright as _sync_playwright
except ImportError:
    pass
else:
    PlaywrightError = _PlaywrightError
    sync_playwright = _sync_playwright

# Default file patterns that warrant a browser sanity check.
# Overlays can override via ``OverlayBase.get_visual_qa_targets()``.
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

MAX_PAGES = 5
PER_PAGE_TIMEOUT_MS = 10_000
TOTAL_TIMEOUT_S = 60
DEFAULT_SCREENSHOT_DIR = ".t3/visual_qa"

# 401/403 are common when an authenticated-only flow is logged out — not a blocker.
_HTTP_ERROR_THRESHOLD = 400
_NON_BLOCKING_STATUSES = frozenset({401, 403})

_TRANSLATION_KEY_RE = re.compile(r"\bapp\.[a-z][a-z0-9_]*(?:\.[a-z0-9_]+){1,}\b", re.IGNORECASE)


class VisualQAUnavailableError(RuntimeError):
    """Raised when Playwright cannot run — gate fails open with a message."""


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


@dataclass(frozen=True, slots=True)
class VisualQAReport:
    targets: list[str]
    pages: list[PageResult] = field(default_factory=list)
    skipped_reason: str = ""
    base_url: str = ""

    @property
    def has_errors(self) -> bool:
        return any(page.errors for page in self.pages)

    @property
    def total_errors(self) -> int:
        return sum(len(page.errors) for page in self.pages)

    def summary(self) -> VisualQASummary:
        """Return a JSON-serialisable snapshot for ``Ticket.extra``."""
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
            base_url=self.base_url,
            pages_checked=len(self.pages),
            errors=self.total_errors,
            details=details,
        )


# ── Detection ────────────────────────────────────────────────────────


def changed_files(repo: str = ".", base: str = "origin/main") -> list[str]:
    """Return paths changed on the current branch vs *base*."""
    out = git.run(repo=repo, args=["diff", "--name-only", f"{base}...HEAD"])
    return [line for line in out.splitlines() if line]


def matches_triggers(paths: list[str], globs: tuple[str, ...] = DEFAULT_TRIGGER_GLOBS) -> list[str]:
    """Return the subset of *paths* matching any glob in *globs*."""
    return [path for path in paths if any(fnmatch.fnmatch(path, glob) for glob in globs)]


def detect_targets(diff: list[str], overlay: OverlayBase | None = None) -> list[str]:
    """Return URL paths to load given the changed files.

    When *overlay* exposes ``get_visual_qa_targets``, defer to it so each
    project can map diff paths to the URLs it cares about.  Otherwise fall
    back to the default trigger globs and return ``["/"]`` if any matched.
    """
    if overlay is not None:
        targets = overlay.get_visual_qa_targets(diff)
        return list(targets[:MAX_PAGES]) if targets else []
    return ["/"] if matches_triggers(diff) else []


# ── Bypass ───────────────────────────────────────────────────────────


def should_run(*, skip_reason: str = "", env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Decide whether to run the gate.

    Returns ``(run, reason)``.  When ``run`` is ``False``, ``reason``
    explains why so the caller can surface it.
    """
    env = env if env is not None else dict(os.environ)
    if skip_reason:
        return (False, f"--skip: {skip_reason}")
    if env.get("T3_VISUAL_QA", "").strip().lower() == "disabled":
        return (False, "T3_VISUAL_QA=disabled")
    return (True, "")


# ── Runner ───────────────────────────────────────────────────────────


def run_check(targets: list[str], base_url: str, screenshot_dir: str = DEFAULT_SCREENSHOT_DIR) -> list[PageResult]:
    """Load each target URL and capture errors + a single screenshot.

    Returns one ``PageResult`` per target.  Raises
    ``VisualQAUnavailableError`` when Playwright cannot start so callers
    can fail open with a clear message rather than blocking the push.
    """
    if sync_playwright is None:
        msg = "playwright is not installed. Run: uv sync && playwright install chromium"
        raise VisualQAUnavailableError(msg)

    out_dir = Path(screenshot_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + TOTAL_TIMEOUT_S
    results: list[PageResult] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            for index, target in enumerate(targets[:MAX_PAGES]):
                if time.monotonic() >= deadline:
                    break
                results.append(_check_one(context, base_url, target, out_dir, index))
            context.close()
            browser.close()
    except PlaywrightError as exc:
        msg = f"playwright failed to launch ({exc.__class__.__name__}): {exc}"
        raise VisualQAUnavailableError(msg) from exc

    return results


def _check_one(context: "BrowserContext", base_url: str, target: str, out_dir: Path, index: int) -> PageResult:
    url = base_url.rstrip("/") + "/" + target.lstrip("/")
    errors: list[PageError] = []
    page = context.new_page()
    page.on("pageerror", lambda exc: errors.append(PageError(url=url, kind="page", message=str(exc))))
    page.on("console", lambda msg: _record_console(errors, url, msg))
    page.on("response", lambda resp: _record_http(errors, url, resp))

    try:
        page.goto(url, timeout=PER_PAGE_TIMEOUT_MS, wait_until="networkidle")
    except PlaywrightError as exc:
        errors.append(PageError(url=url, kind="page", message=f"navigation failed: {exc}"))

    body_text = ""
    with contextlib.suppress(Exception):
        body_text = page.locator("body").inner_text(timeout=2_000)
    errors.extend(
        PageError(url=url, kind="translation", message=f"raw key in DOM: {match}")
        for match in _TRANSLATION_KEY_RE.findall(body_text)
    )

    screenshot_path = ""
    slug = _slug(target, index)
    candidate = out_dir / f"{slug}.png"
    try:
        page.screenshot(path=str(candidate), full_page=False, animations="disabled")
        screenshot_path = str(candidate)
    except PlaywrightError:
        screenshot_path = ""

    page.close()
    return PageResult(url=url, screenshot_path=screenshot_path, errors=errors)


def _record_console(errors: list[PageError], url: str, msg: object) -> None:
    if getattr(msg, "type", "") != "error":
        return
    errors.append(PageError(url=url, kind="console", message=getattr(msg, "text", "")))


def _record_http(errors: list[PageError], url: str, resp: object) -> None:
    status = getattr(resp, "status", 0)
    if status < _HTTP_ERROR_THRESHOLD or status in _NON_BLOCKING_STATUSES:
        return
    errors.append(PageError(url=url, kind="http", message=f"HTTP {status}: {getattr(resp, 'url', '')}"))


def _slug(target: str, index: int) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")
    return f"{index:02d}-{cleaned or 'root'}"


# ── Orchestration ────────────────────────────────────────────────────


def evaluate(
    *,
    diff: list[str],
    overlay: OverlayBase | None,
    base_url: str,
    skip_reason: str = "",
    env: dict[str, str] | None = None,
) -> VisualQAReport:
    """Run the full gate end to end and return the report.

    Single entry point used by the shipping gate.  Returns an empty
    report (``has_errors == False``) when the gate is bypassed, when no
    frontend changes are detected, or when Playwright is unavailable.
    Callers decide what to do with the report (block, warn, record).
    """
    run, bypass_reason = should_run(skip_reason=skip_reason, env=env)
    if not run:
        return VisualQAReport(targets=[], skipped_reason=bypass_reason)

    targets = detect_targets(diff, overlay)
    if not targets:
        return VisualQAReport(targets=[], skipped_reason="no frontend changes")

    try:
        pages = run_check(targets, base_url)
    except VisualQAUnavailableError as exc:
        return VisualQAReport(targets=targets, skipped_reason=str(exc), base_url=base_url)

    return VisualQAReport(targets=targets, pages=pages, base_url=base_url)


# ── Report ───────────────────────────────────────────────────────────


def format_report(report: VisualQAReport) -> str:
    """Render a ``## Visual QA`` markdown section for the PR description."""
    lines = ["## Visual QA", ""]
    if report.skipped_reason:
        lines.append(f"_skipped: {report.skipped_reason}_")
        return "\n".join(lines) + "\n"
    if not report.targets:
        lines.append("_no frontend changes detected_")
        return "\n".join(lines) + "\n"

    base = report.base_url
    if base:
        lines.extend((f"_base url: {base}_", ""))
    lines.extend(
        (
            f"Checked {len(report.pages)} page(s) — {report.total_errors} finding(s).",
            "",
        ),
    )
    for page in report.pages:
        path = page.url.removeprefix(base) or "/" if base else page.url
        marker = ":x:" if page.errors else ":white_check_mark:"
        lines.append(f"### {marker} `{path}`")
        if page.screenshot_path:
            lines.append(f"![{path}]({page.screenshot_path})")
        if page.errors:
            lines.append("")
            lines.extend(f"- **{error.kind}**: {error.message}" for error in page.errors)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

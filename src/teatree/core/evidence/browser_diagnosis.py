"""chrome-devtools-mcp — teatree's default browser tool.

Browser-visible breakage — a blank render, a failed XHR, a console error — is
diagnosed *in the browser* (network / console / DOM), not guessed from the
Python side; and agentic browser work (navigate, click, fill, upload) drives the
page the same way. Google's ``chrome-devtools-mcp`` server exposes both over CDP
with no claude.ai account or extension pairing, so it is the default browser tool
— registered with a headless browser.

This module is the single source of truth for *what* that registration is — the
server name and the ``claude mcp add`` command — consumed by the
``t3 mcp browser-diagnosis`` CLI. There is deliberately no gate code here: this
server is a diagnostic and interaction aid, never an enforcement path. Perf/trace
*enforcement* stays in the deterministic Playwright lane.
"""

from dataclasses import dataclass

CHROME_DEVTOOLS_SERVER_NAME = "chrome-devtools"
# ``npx`` avoids a global install; ``@latest`` is upstream's recommended pin.
CHROME_DEVTOOLS_LAUNCH: tuple[str, ...] = ("npx", "-y", "chrome-devtools-mcp@latest")
# Upstream defaults to a visible Chrome, so headless has to be asked for explicitly.
CHROME_DEVTOOLS_HEADLESS_FLAG = "--headless=true"


def chrome_devtools_launch() -> tuple[str, ...]:
    return (*CHROME_DEVTOOLS_LAUNCH, CHROME_DEVTOOLS_HEADLESS_FLAG)


def chrome_devtools_add_command() -> str:
    launch = " ".join(chrome_devtools_launch())
    return f"claude mcp add {CHROME_DEVTOOLS_SERVER_NAME} -- {launch}"


@dataclass(frozen=True, slots=True)
class BrowserDiagnosisRegistration:
    """Whether chrome-devtools-mcp (the default browser tool) is enabled, and how to add it."""

    enabled: bool
    server_name: str
    add_command: str
    message: str


def resolve_browser_diagnosis() -> BrowserDiagnosisRegistration:
    """Resolve the browser-diagnosis registration.

    Returns the ``claude mcp add`` command that registers the headless server.
    """
    add_command = chrome_devtools_add_command()
    return BrowserDiagnosisRegistration(
        enabled=True,
        server_name=CHROME_DEVTOOLS_SERVER_NAME,
        add_command=add_command,
        message=(
            f"chrome-devtools-mcp ('{CHROME_DEVTOOLS_SERVER_NAME}') is the default browser tool. Register it with:\n"
            f"  {add_command}\n"
            "Use it to drive and inspect a deployed page (navigate/click/fill, network/console/DOM) before "
            "proposing a root cause; perf/trace enforcement stays in the deterministic Playwright lane."
        ),
    )


__all__ = [
    "CHROME_DEVTOOLS_LAUNCH",
    "CHROME_DEVTOOLS_SERVER_NAME",
    "BrowserDiagnosisRegistration",
    "resolve_browser_diagnosis",
]

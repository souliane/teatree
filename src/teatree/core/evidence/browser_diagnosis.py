"""chrome-devtools-mcp — teatree's default browser tool.

Browser-visible breakage — a blank render, a failed XHR, a console error — is
diagnosed *in the browser* (network / console / DOM), not guessed from the
Python side; and agentic browser work (navigate, click, fill, upload) drives the
page the same way. Google's ``chrome-devtools-mcp`` server exposes both over CDP
with no claude.ai account or extension pairing, so it is the default browser tool
— registered whenever the ``chrome_devtools_mcp_enabled`` flag is set (default
on).

This module is the single source of truth for *what* that registration is — the
server name and the ``claude mcp add`` command — consumed by the
``t3 mcp browser-diagnosis`` CLI. There is deliberately no gate code here: this
server is a diagnostic and interaction aid, never an enforcement path. Perf/trace
*enforcement* stays in the deterministic Playwright lane.
"""

from dataclasses import dataclass

from teatree.config import get_effective_settings

CHROME_DEVTOOLS_SERVER_NAME = "chrome-devtools"
# ``npx`` avoids a global install; ``@latest`` is upstream's recommended pin.
CHROME_DEVTOOLS_LAUNCH: tuple[str, ...] = ("npx", "-y", "chrome-devtools-mcp@latest")


@dataclass(frozen=True, slots=True)
class BrowserDiagnosisRegistration:
    """Whether chrome-devtools-mcp (the default browser tool) is enabled, and how to add it."""

    enabled: bool
    server_name: str
    add_command: str
    message: str


def _add_command() -> str:
    return f"claude mcp add {CHROME_DEVTOOLS_SERVER_NAME} -- {' '.join(CHROME_DEVTOOLS_LAUNCH)}"


def resolve_browser_diagnosis(overlay_name: str | None = None) -> BrowserDiagnosisRegistration:
    """Resolve the browser-diagnosis registration for *overlay_name*.

    Reads the ``chrome_devtools_mcp_enabled`` flag (per-overlay overridable).
    When off, returns ``enabled=False`` with the exact command to turn it on;
    when on, returns the ``claude mcp add`` command that registers the server.
    """
    enabled = bool(get_effective_settings(overlay_name).chrome_devtools_mcp_enabled)
    if not enabled:
        return BrowserDiagnosisRegistration(
            enabled=False,
            server_name=CHROME_DEVTOOLS_SERVER_NAME,
            add_command=_add_command(),
            message=(
                f"Browser-diagnosis MCP ('{CHROME_DEVTOOLS_SERVER_NAME}') is disabled. Enable it with "
                "`t3 <overlay> config_setting set chrome_devtools_mcp_enabled true`, then re-run this "
                "command for the registration line."
            ),
        )
    return BrowserDiagnosisRegistration(
        enabled=True,
        server_name=CHROME_DEVTOOLS_SERVER_NAME,
        add_command=_add_command(),
        message=(
            f"chrome-devtools-mcp ('{CHROME_DEVTOOLS_SERVER_NAME}') is the default browser tool. Register it with:\n"
            f"  {_add_command()}\n"
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

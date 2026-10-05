"""Browser-diagnosis MCP registration resolver.

Chrome DevTools MCP is the browser tool. The resolver emits the exact
``claude mcp add`` registration line.
"""

from django.test import SimpleTestCase, TestCase

from teatree.core.evidence.browser_diagnosis import (
    CHROME_DEVTOOLS_LAUNCH,
    CHROME_DEVTOOLS_SERVER_NAME,
    chrome_devtools_add_command,
    chrome_devtools_launch,
    resolve_browser_diagnosis,
)


class TestResolveBrowserDiagnosis(TestCase):
    def test_enabled_by_default(self) -> None:
        registration = resolve_browser_diagnosis()
        assert registration.enabled is True
        assert (
            registration.add_command
            == f"claude mcp add {CHROME_DEVTOOLS_SERVER_NAME} -- npx -y chrome-devtools-mcp@latest --headless=true"
        )
        assert registration.add_command in registration.message


class TestBrowserDiagnosisHeadless(TestCase):
    """teatree runs 100% headless, so the registered server must never open a visible Chrome.

    Upstream chrome-devtools-mcp defaults to a visible browser, so headless is explicit.
    """

    def test_headless_flag_passed(self) -> None:
        assert "--headless=true" in resolve_browser_diagnosis().add_command


class TestChromeDevtoolsLaunchArgs(SimpleTestCase):
    """The launch args are the seam every caller renders from — the doctor included."""

    def test_headless_appended_by_default(self) -> None:
        assert chrome_devtools_launch() == (*CHROME_DEVTOOLS_LAUNCH, "--headless=true")

    def test_add_command_renders_the_launch_args(self) -> None:
        assert chrome_devtools_add_command() == f"claude mcp add {CHROME_DEVTOOLS_SERVER_NAME} -- " + " ".join(
            chrome_devtools_launch()
        )

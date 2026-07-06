"""Browser-diagnosis MCP registration resolver.

The flag ``chrome_devtools_mcp_enabled`` is ON by default — chrome-devtools-mcp is
the default browser tool. The resolver emits the exact ``claude mcp add``
registration line; an operator who opts out gets the re-enable hint instead.
"""

from django.test import TestCase

from teatree.core.evidence.browser_diagnosis import CHROME_DEVTOOLS_SERVER_NAME, resolve_browser_diagnosis
from teatree.core.models.config_setting import ConfigSetting


class TestResolveBrowserDiagnosis(TestCase):
    def test_enabled_by_default(self) -> None:
        registration = resolve_browser_diagnosis(None)
        assert registration.enabled is True
        assert (
            registration.add_command
            == f"claude mcp add {CHROME_DEVTOOLS_SERVER_NAME} -- npx -y chrome-devtools-mcp@latest"
        )
        assert registration.add_command in registration.message

    def test_disabled_when_opted_out(self) -> None:
        ConfigSetting.objects.set_value("chrome_devtools_mcp_enabled", value=False)
        registration = resolve_browser_diagnosis(None)
        assert registration.enabled is False
        assert CHROME_DEVTOOLS_SERVER_NAME in registration.message
        assert "chrome_devtools_mcp_enabled true" in registration.message

    def test_add_command_names_the_server_regardless_of_flag(self) -> None:
        # The registration command is stable; only `enabled` and the framing flip.
        on = resolve_browser_diagnosis(None)
        ConfigSetting.objects.set_value("chrome_devtools_mcp_enabled", value=False)
        off = resolve_browser_diagnosis(None)
        assert off.add_command == on.add_command

from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from mcp.server.mcpserver.exceptions import ToolError

from teatree.mcp.config_write_tools import _config_setting_set, refuse_reason


def test_governed_config_key_never_reaches_the_command() -> None:
    with (
        patch("teatree.mcp.config_write_tools.run_command") as command,
        pytest.raises(ToolError, match="not MCP-settable"),
    ):
        async_to_sync(_config_setting_set)("send_proxy_allowlist", "[]")
    command.assert_not_called()
    assert refuse_reason("send_proxy_allowlist")


def test_plain_key_uses_the_same_command_seam() -> None:
    with patch("teatree.mcp.config_write_tools.run_command") as command:
        result = async_to_sync(_config_setting_set)("loop_cadence_seconds", "42", overlay="demo")

    assert result == {"ok": True, "key": "loop_cadence_seconds", "overlay": "demo"}
    command.assert_called_once_with("config_setting", "set", "loop_cadence_seconds", "42", overlay="demo")

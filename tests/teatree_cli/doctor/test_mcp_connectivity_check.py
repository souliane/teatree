"""``_check_mcp_connectivity`` — the `t3 doctor` enabled-MCP gate (#2282)."""

from unittest.mock import patch

from teatree.cli.doctor.checks_mcp import _check_mcp_connectivity
from teatree.core.mcp_connectivity import McpConnectivityOutcome


def _outcome(**kwargs) -> McpConnectivityOutcome:
    return McpConnectivityOutcome(**kwargs)


class TestMcpConnectivityDoctorCheck:
    def test_all_connected_is_ok_silent(self, capsys):
        with patch(
            "teatree.core.mcp_connectivity.check_mcp_connectivity",
            return_value=_outcome(ok=True),
        ):
            assert _check_mcp_connectivity() is True
        assert capsys.readouterr().out == ""

    def test_disconnected_server_fails_loudly(self, capsys):
        outcome = _outcome(
            ok=False,
            findings=["MCP server 'claude.ai Notion' is enabled but NOT connected. Reconnect it: ..."],
        )
        with patch("teatree.core.mcp_connectivity.check_mcp_connectivity", return_value=outcome):
            assert _check_mcp_connectivity() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "claude.ai Notion" in out

    def test_degraded_probe_is_warn_not_fail(self, capsys):
        outcome = _outcome(ok=True, degraded=True, findings=["Could not live-probe MCP connectivity ..."])
        with patch("teatree.core.mcp_connectivity.check_mcp_connectivity", return_value=outcome):
            assert _check_mcp_connectivity() is True
        assert "WARN" in capsys.readouterr().out

    def test_crash_degrades_to_warn_not_abort(self, capsys):
        with patch(
            "teatree.core.mcp_connectivity.check_mcp_connectivity",
            side_effect=RuntimeError("boom"),
        ):
            assert _check_mcp_connectivity() is True
        assert "WARN" in capsys.readouterr().out

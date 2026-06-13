"""`t3 setup recover-account-switch` CLI surface (#1916)."""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.account_switch_recover import recover_account_switch
from teatree.core.account_switch import AccountSwitchOutcome, ConnectorProbeResult
from teatree.core.mcp_connectivity import McpConnectivityOutcome

runner = CliRunner()


def _app():
    import typer  # noqa: PLC0415

    app = typer.Typer()
    app.command("recover-account-switch")(recover_account_switch)
    return app


def _outcome(*, switched: bool, probes: tuple[ConnectorProbeResult, ...] = ()) -> AccountSwitchOutcome:
    return AccountSwitchOutcome(
        current_fingerprint="uuid-bbbbbbbb",
        previous_fingerprint="uuid-aaaaaaaa",
        switched=switched,
        probes=probes,
    )


@pytest.fixture(autouse=True)
def _no_django(monkeypatch):
    monkeypatch.setattr("teatree.cli.account_switch_recover.ensure_django", lambda: None)


@pytest.fixture(autouse=True)
def _mcp_clean(monkeypatch):
    """Default the MCP connectivity check to clean so account-switch tests stay focused.

    The recover path now also re-runs the #2282 enabled-MCP check; tests that
    care about it override this with their own ``check_mcp_connectivity`` patch.
    """
    monkeypatch.setattr(
        "teatree.core.mcp_connectivity.check_mcp_connectivity",
        lambda: McpConnectivityOutcome(ok=True),
    )


class TestRecoverAccountSwitchCommand:
    def test_no_switch_exits_zero(self):
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            return_value=_outcome(switched=False),
        ):
            result = runner.invoke(_app(), [])
        assert result.exit_code == 0
        assert "No account switch" in result.output

    def test_switch_all_reachable_exits_zero(self):
        probes = (ConnectorProbeResult(name="slack", reachable=True),)
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            return_value=_outcome(switched=True, probes=probes),
        ):
            result = runner.invoke(_app(), [])
        assert result.exit_code == 0
        assert "All connectors reachable" in result.output
        assert "slack: reachable" in result.output

    def test_switch_unreachable_exits_nonzero(self):
        probes = (ConnectorProbeResult(name="slack", reachable=False, detail="invalid_auth"),)
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            return_value=_outcome(switched=True, probes=probes),
        ):
            result = runner.invoke(_app(), [])
        assert result.exit_code == 1
        assert "UNREACHABLE" in result.output
        assert "invalid_auth" in result.output

    def test_switch_reachable_but_mcp_disconnected_exits_nonzero(self, monkeypatch):
        """AC2: the same enabled-MCP check re-runs on the account-switch path."""
        probes = (ConnectorProbeResult(name="slack", reachable=True),)
        monkeypatch.setattr(
            "teatree.core.mcp_connectivity.check_mcp_connectivity",
            lambda: McpConnectivityOutcome(
                ok=False,
                findings=["MCP server 'claude.ai Notion' is enabled but NOT connected. Reconnect it: ..."],
            ),
        )
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            return_value=_outcome(switched=True, probes=probes),
        ):
            result = runner.invoke(_app(), [])
        assert result.exit_code == 1
        assert "claude.ai Notion" in result.output
        assert "All connectors reachable" not in result.output

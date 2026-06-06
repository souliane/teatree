"""`t3 setup recover-account-switch` CLI surface (#1916)."""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.account_switch_recover import recover_account_switch
from teatree.core.account_switch import AccountSwitchOutcome, ConnectorProbeResult

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

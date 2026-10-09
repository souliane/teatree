"""`t3 setup recover-account-switch` CLI surface (#1916)."""

from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.account_switch_recover import recover_account_switch
from teatree.core.account_switch import AccountSwitchOutcome

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    app.command("recover-account-switch")(recover_account_switch)
    return app


def _outcome(*, switched: bool) -> AccountSwitchOutcome:
    return AccountSwitchOutcome(
        current_fingerprint="uuid-bbbbbbbb",
        previous_fingerprint="uuid-aaaaaaaa",
        switched=switched,
        token_health_rows_expired=4 if switched else 0,
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

    def test_a_switch_reports_what_it_invalidated_and_exits_zero(self):
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            return_value=_outcome(switched=True),
        ):
            result = runner.invoke(_app(), [])
        assert result.exit_code == 0
        assert "Account switch: uuid-aaa… → uuid-bbb…" in result.output
        assert "token health expired (4 row(s)" in result.output
        assert "new account recorded" in result.output

    def test_the_command_takes_no_open_flag(self):
        result = runner.invoke(_app(), ["--open"])
        assert result.exit_code == 2

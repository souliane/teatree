"""``_check_account_switch`` — the `t3 doctor` account-switch gate (#1916)."""

from unittest.mock import patch

from teatree.cli.doctor.checks_session import _check_account_switch
from teatree.core.account_switch import AccountSwitchOutcome


def _outcome(*, switched: bool) -> AccountSwitchOutcome:
    return AccountSwitchOutcome(
        current_fingerprint="uuid-bbbbbbbb",
        previous_fingerprint="uuid-aaaaaaaa",
        switched=switched,
        token_health_rows_expired=2 if switched else 0,
    )


class TestAccountSwitchDoctorCheck:
    def test_no_switch_is_ok_silent(self, capsys):
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            return_value=_outcome(switched=False),
        ):
            assert _check_account_switch() is True
        assert capsys.readouterr().out == ""

    def test_a_recovered_switch_is_ok_with_the_expired_token_health_count(self, capsys):
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            return_value=_outcome(switched=True),
        ):
            assert _check_account_switch() is True
        out = capsys.readouterr().out
        assert out.startswith("OK    Claude account switch recovered (uuid-aaa… → uuid-bbb…)")
        assert "token health expired (2 row(s))" in out
        assert "FAIL" not in out

    def test_crash_degrades_to_warn_not_abort(self, capsys):
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            side_effect=RuntimeError("boom"),
        ):
            assert _check_account_switch() is True
        assert "WARN" in capsys.readouterr().out

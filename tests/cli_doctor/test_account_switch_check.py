"""``_check_account_switch`` — the `t3 doctor` account-switch gate (#1916)."""

from unittest.mock import patch

from teatree.cli.doctor.checks_session import _check_account_switch
from teatree.core.account_switch import AccountSwitchOutcome, ConnectorProbeResult


def _outcome(*, switched: bool, probes: tuple[ConnectorProbeResult, ...] = ()) -> AccountSwitchOutcome:
    return AccountSwitchOutcome(
        current_fingerprint="uuid-bbbbbbbb",
        previous_fingerprint="uuid-aaaaaaaa",
        switched=switched,
        probes=probes,
    )


class TestAccountSwitchDoctorCheck:
    def test_no_switch_is_ok_silent(self, capsys):
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            return_value=_outcome(switched=False),
        ):
            assert _check_account_switch() is True
        assert capsys.readouterr().out == ""

    def test_switch_all_reachable_is_ok(self, capsys):
        probes = (ConnectorProbeResult(name="slack", reachable=True),)
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            return_value=_outcome(switched=True, probes=probes),
        ):
            assert _check_account_switch() is True
        assert "OK" in capsys.readouterr().out

    def test_switch_with_unreachable_connector_fails(self, capsys):
        probes = (ConnectorProbeResult(name="slack", reachable=False, detail="invalid_auth"),)
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            return_value=_outcome(switched=True, probes=probes),
        ):
            assert _check_account_switch() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "slack" in out
        assert "invalid_auth" in out

    def test_crash_degrades_to_warn_not_abort(self, capsys):
        with patch(
            "teatree.core.account_switch.detect_and_recover_account_switch",
            side_effect=RuntimeError("boom"),
        ):
            assert _check_account_switch() is True
        assert "WARN" in capsys.readouterr().out

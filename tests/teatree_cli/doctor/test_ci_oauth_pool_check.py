"""A depleted CI OAuth fleet must not leave doctor green."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from teatree.cli.doctor.checks_ci_oauth_pool import check_ci_oauth_pool, check_ci_oauth_pool_membership
from teatree.cli.doctor.run_checks import _run_loop_intent_gates
from teatree.core.models.config_setting import ConfigSetting
from teatree.credential_config import LIST_SETTING, TokenKind
from teatree.token_rows import TokenSource, TokenStatus


def _row(account: str, status: TokenStatus) -> SimpleNamespace:
    return SimpleNamespace(account=account, kind=TokenKind.OAUTH, source=TokenSource.STORE, status=status)


def test_all_configured_oauth_accounts_near_exhausted_fail(capsys) -> None:
    assert check_ci_oauth_pool(rows=[_row("a", TokenStatus.EXHAUSTED), _row("b", TokenStatus.WARNING)]) is False
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert "a" in output
    assert "b" in output


def test_one_healthy_oauth_account_keeps_doctor_green(capsys) -> None:
    assert check_ci_oauth_pool(rows=[_row("a", TokenStatus.EXHAUSTED), _row("b", TokenStatus.HEALTHY)]) is True
    assert "FAIL" not in capsys.readouterr().out


def test_uncached_account_is_not_misreported_as_exhausted(capsys) -> None:
    assert check_ci_oauth_pool(rows=[_row("a", TokenStatus.UNCACHED)]) is True
    assert "WARN" in capsys.readouterr().out


def test_doctor_run_includes_the_pool_check() -> None:
    assert "check_ci_oauth_pool" in _run_loop_intent_gates.__code__.co_names


def test_pool_membership_drift_fails_and_names_missing_and_stale(capsys) -> None:
    rows = [_row("primary", TokenStatus.HEALTHY), _row("spare", TokenStatus.HEALTHY)]
    assert check_ci_oauth_pool_membership(rows, ("primary", "retired")) is False
    output = capsys.readouterr().out
    assert "spare" in output
    assert "retired" in output


def test_unknown_pool_membership_fails_loud(capsys) -> None:
    assert check_ci_oauth_pool_membership([_row("primary", TokenStatus.HEALTHY)], None) is False
    assert "FAIL" in capsys.readouterr().out


def test_matching_pool_membership_passes(capsys) -> None:
    assert check_ci_oauth_pool_membership([_row("primary", TokenStatus.HEALTHY)], ("primary",)) is True
    assert "FAIL" not in capsys.readouterr().out


def test_pool_membership_checks_allowlist_instead_of_all_stored_accounts(capsys) -> None:
    rows = [_row("primary", TokenStatus.HEALTHY), _row("spare", TokenStatus.HEALTHY)]
    assert check_ci_oauth_pool_membership(rows[:1], ("primary",)) is True
    assert "FAIL" not in capsys.readouterr().out

    rows = [_row("primary", TokenStatus.EXHAUSTED), _row("spare", TokenStatus.HEALTHY)]
    with (
        patch("teatree.token_report.TokenReport.rows", return_value=rows),
        patch(
            "teatree.cli.doctor.checks_ci_oauth_pool.get_effective_settings",
            return_value=SimpleNamespace(eval_ci_account_allowlist=("primary",)),
        ),
        patch("teatree.ci_oauth_switch.CiAccountSwitcher.active_pool_accounts", return_value=("primary",)),
    ):
        assert check_ci_oauth_pool() is False
    assert "primary" in capsys.readouterr().out


class LiveDoctorAllowlistTest(TestCase):
    def test_an_install_with_no_oauth_account_stays_green_without_an_allowlist(self) -> None:
        assert check_ci_oauth_pool() is True

    def test_oauth_accounts_without_an_allowlist_fail(self) -> None:
        ConfigSetting.objects.set_value(LIST_SETTING[TokenKind.OAUTH], ["anthropic/oauth/a"])
        with patch("teatree.cli.doctor.checks_ci_oauth_pool.typer.echo") as echo:
            assert check_ci_oauth_pool() is False
        assert any("allowlist is empty" in call.args[0] for call in echo.call_args_list)

    def test_allowlisted_account_without_stored_row_fails_even_when_pool_matches(self) -> None:
        rows = [_row("spare", TokenStatus.HEALTHY)]
        with (
            patch("teatree.token_report.TokenReport.rows", return_value=rows),
            patch(
                "teatree.cli.doctor.checks_ci_oauth_pool.get_effective_settings",
                return_value=SimpleNamespace(eval_ci_account_allowlist=("primary",)),
            ),
            patch("teatree.ci_oauth_switch.CiAccountSwitcher.active_pool_accounts", return_value=("primary",)),
            patch("teatree.cli.doctor.checks_ci_oauth_pool.typer.echo") as echo,
        ):
            assert check_ci_oauth_pool() is False
        assert any("no stored OAuth row" in call.args[0] for call in echo.call_args_list)

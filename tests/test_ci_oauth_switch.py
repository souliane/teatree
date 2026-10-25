"""Tests for the CI OAuth account auto-switch (``teatree.ci_oauth_switch``).

The switcher reads the SAME rows ``t3 tokens`` renders, ranks the eligible accounts on
headroom as of the run's start, and points the ``CLAUDE_CODE_OAUTH_TOKEN`` repo secret at
the winner. These tests drive real :class:`~teatree.token_report.TokenReport` rows built
from canned health (no network, no ``pass``) through a recording fake ``gh``, and assert
the selection rule, the loud all-exhausted refusal, the already-optimal no-op, and — the
load-bearing invariant — that a token value never reaches any captured output.
"""

import datetime as dt
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.ci_oauth_switch import (
    CI_ACCOUNT_VARIABLE,
    CI_OAUTH_POOL_SECRET,
    CI_OAUTH_POOL_VARIABLE,
    CI_OAUTH_SECRET,
    WEIGHT_5H,
    WEIGHT_7D,
    CiAccountSwitcher,
    CiAccountSwitchError,
    GhClient,
    NoEligibleAccountError,
    select_account,
)
from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting
from teatree.credential_config import TokenKind
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.token_report import TokenAccountRow, TokenSource, TokenStatus

RUN_START = dt.datetime(2026, 7, 21, 12, 0, tzinfo=dt.UTC)
SECRET_TOKEN = "sk-" + "ant-oat01-never-printed-anywhere"

SOULIANE = "anthropic/primary@example.com/oauth-token"
SPARE_ACCOUNT = "anthropic/spare@example.com/oauth-token"
SPENT_ACCOUNT = "anthropic/spent@example.com/oauth-token"


def _row(
    account: str,
    *,
    u5h: float,
    u7d: float,
    status: TokenStatus = TokenStatus.HEALTHY,
) -> TokenAccountRow:
    """One OAuth ``pass`` row exactly as ``TokenReport`` builds it.

    Both windows reset AFTER :data:`RUN_START`, so a test wanting the reset projection
    moves the run's start forward rather than backdating a reset.
    """
    return TokenAccountRow(
        account=account,
        kind=TokenKind.OAUTH,
        source=TokenSource.STORE,
        scopes=("teatree",),
        organization_id="org-test",
        utilization_5h=u5h,
        utilization_7d=u7d,
        weekly_reset=RUN_START + dt.timedelta(days=3),
        status=status,
        next_window_reset=RUN_START + dt.timedelta(hours=2),
    )


def _live_rows() -> list[TokenAccountRow]:
    """The three accounts from the directive's live ``t3 tokens`` capture."""
    return [
        _row(SOULIANE, u5h=0.46, u7d=0.82),
        _row(SPARE_ACCOUNT, u5h=0.87, u7d=0.13, status=TokenStatus.WARNING),
        _row(SPENT_ACCOUNT, u5h=0.0, u7d=1.0, status=TokenStatus.EXHAUSTED),
    ]


SECRET_SET_FLAGS = frozenset({"--repo", "-R"})


class FakeGh:
    """A recording ``gh`` stand-in: canned variable value, captured argv + stdin.

    ``secret set`` accepts only this switcher's stdin-only argv shape; the installed-gh
    test checks the real client's flag set.
    """

    def __init__(
        self, *, active: str = "", pool_accounts: str | None = None, fail_on: str = "", fail_output: str = "boom"
    ) -> None:
        self.active = active
        self.pool_accounts = pool_accounts
        self.fail_on = fail_on
        self.fail_output = fail_output
        self.calls: list[list[str]] = []
        self.stdin: list[str | None] = []

    def __call__(self, args: list[str], *, stdin_text: str | None = None) -> tuple[int, str]:
        self.calls.append(list(args))
        self.stdin.append(stdin_text)
        if args[:2] == ["secret", "set"]:
            unknown = [part for part in args[3:] if part.startswith("-") and part not in SECRET_SET_FLAGS]
            if unknown:
                return 1, f"unknown flag: {unknown[0]}"
        if self.fail_on and self.fail_on in args:
            return 1, self.fail_output
        if args[0] == "api":
            if "EVAL_OAUTH_POOL_ACCOUNTS" in args[1]:
                return (0, self.pool_accounts) if self.pool_accounts is not None else (1, "not found")
            return (0, self.active) if self.active else (1, "not found")
        return 0, ""

    @property
    def wrote_secret(self) -> bool:
        return any(call[:2] == ["secret", "set"] for call in self.calls)

    @property
    def wrote_variable(self) -> bool:
        return any(call[:2] == ["variable", "set"] for call in self.calls)


def _switcher(gh: GhClient) -> CiAccountSwitcher:
    return CiAccountSwitcher(repo="souliane/teatree", gh=gh, secret_reader=lambda _account: SECRET_TOKEN)


class TestPoolReconciliation:
    def test_unset_allowlist_refuses_without_touching_github(self) -> None:
        gh = FakeGh()
        with pytest.raises(RuntimeError, match="allowlist"):
            _switcher(gh).reconcile_pool(_live_rows())
        assert gh.calls == []

    def test_only_allowlisted_accounts_are_published(self) -> None:
        gh = FakeGh()
        switcher = CiAccountSwitcher(repo="souliane/teatree", gh=gh, secret_reader=lambda account: account)
        accounts = switcher.reconcile_pool(_live_rows(), allowlist=(SOULIANE,))
        assert accounts == (SOULIANE,)
        assert gh.stdin[0] == SOULIANE

    def test_allowlisted_account_missing_from_store_refuses_before_writing(self) -> None:
        gh = FakeGh()
        with pytest.raises(CiAccountSwitchError, match="not configured"):
            _switcher(gh).reconcile_pool(_live_rows(), allowlist=("missing",))
        assert gh.calls == []

    def test_readable_pool_membership_is_read_back(self) -> None:
        gh = FakeGh(pool_accounts=json.dumps([SOULIANE, SPARE_ACCOUNT]))
        assert _switcher(gh).active_pool_accounts() == (SOULIANE, SPARE_ACCOUNT)

    def test_missing_pool_membership_is_unknown_not_empty(self) -> None:
        assert _switcher(FakeGh()).active_pool_accounts() is None

    def test_full_configured_set_including_exhausted_is_written_as_one_secret(self) -> None:
        gh = FakeGh()
        tokens = {SOULIANE: "sk-token-one", SPARE_ACCOUNT: "sk-token-two", SPENT_ACCOUNT: "sk-token-three"}
        switcher = CiAccountSwitcher(repo="souliane/teatree", gh=gh, secret_reader=tokens.__getitem__)

        accounts = switcher.reconcile_pool(_live_rows(), allowlist=tuple(tokens))

        assert accounts == tuple(sorted(tokens))
        pool_writes = [i for i, args in enumerate(gh.calls) if args[:3] == ["secret", "set", CI_OAUTH_POOL_SECRET]]
        assert len(pool_writes) == 1
        assert set((gh.stdin[pool_writes[0]] or "").splitlines()) == set(tokens.values())
        assert all(token not in " ".join(args) for token in tokens.values() for args in gh.calls)

    def test_removal_replaces_pool_with_only_remaining_accounts(self) -> None:
        gh = FakeGh()
        tokens = {SOULIANE: "sk-token-one", SPARE_ACCOUNT: "sk-token-two"}
        switcher = CiAccountSwitcher(repo="souliane/teatree", gh=gh, secret_reader=tokens.__getitem__)

        switcher.reconcile_pool(_live_rows()[:2], allowlist=tuple(tokens))
        assert switcher.reconcile_pool(_live_rows()[:1], allowlist=(SOULIANE,)) == (SOULIANE,)

        bodies = [body for args, body in zip(gh.calls, gh.stdin, strict=True) if CI_OAUTH_POOL_SECRET in args]
        assert bodies == ["sk-token-one\nsk-token-two", "sk-token-one"]

    def test_missing_token_refuses_before_overwriting_existing_pool(self) -> None:
        gh = FakeGh()
        switcher = CiAccountSwitcher(repo="souliane/teatree", gh=gh, secret_reader=lambda _: "")

        with pytest.raises(RuntimeError, match="no token stored"):
            switcher.reconcile_pool(_live_rows(), allowlist=(SOULIANE,))
        assert not gh.wrote_secret

    def test_failed_pool_write_reports_gh_error_without_leaking_token(self) -> None:
        gh = FakeGh(fail_on=CI_OAUTH_POOL_SECRET, fail_output=f"permission denied: {SECRET_TOKEN}")
        switcher = _switcher(gh)

        with pytest.raises(RuntimeError) as caught:
            switcher.reconcile_pool(_live_rows(), allowlist=(SOULIANE,))
        assert "permission denied" in str(caught.value)
        assert SECRET_TOKEN not in str(caught.value)
        assert not gh.wrote_variable

    def test_failed_pool_write_redacts_multiple_overlapping_tokens(self) -> None:
        short = "secret-value"
        long = "secret-value-long"
        tokens = {SOULIANE: short, SPARE_ACCOUNT: long}
        gh = FakeGh(fail_on=CI_OAUTH_POOL_SECRET, fail_output=f"write failed: {short} and {long}")
        switcher = CiAccountSwitcher(repo="souliane/teatree", gh=gh, secret_reader=tokens.__getitem__)

        with pytest.raises(CiAccountSwitchError) as caught:
            switcher.reconcile_pool(_live_rows(), allowlist=tuple(tokens))

        assert short not in str(caught.value)
        assert long not in str(caught.value)
        assert "-long" not in str(caught.value)
        assert "write failed" in str(caught.value)
        assert not gh.wrote_variable


def _switch_secret(gh: GhClient) -> None:
    _switcher(gh).switch(_live_rows(), run_start=RUN_START, allowlist=(SOULIANE,))


def _reconcile_pool_secret(gh: GhClient) -> None:
    _switcher(gh).reconcile_pool(_live_rows(), allowlist=(SOULIANE,))


SECRET_WRITES: list[tuple[str, Callable[[GhClient], None]]] = [
    (CI_OAUTH_SECRET, _switch_secret),
    (CI_OAUTH_POOL_SECRET, _reconcile_pool_secret),
]


def _write_custom_token(secret: str, gh: GhClient, token: str) -> None:
    switcher = CiAccountSwitcher(repo="souliane/teatree", gh=gh, secret_reader=lambda _account: token)
    if secret == CI_OAUTH_SECRET:
        switcher.switch(_live_rows(), run_start=RUN_START, allowlist=(SOULIANE,))
    else:
        switcher.reconcile_pool(_live_rows(), allowlist=(SOULIANE,))


class TestSecretSetArgv:
    @pytest.mark.parametrize(("secret", "write"), SECRET_WRITES)
    def test_the_value_rides_on_stdin_with_no_body_flag(self, secret: str, write: Callable[[GhClient], None]) -> None:
        gh = FakeGh(active=SPENT_ACCOUNT)

        write(gh)

        index = next(i for i, call in enumerate(gh.calls) if call[:2] == ["secret", "set"])
        receipt = CI_ACCOUNT_VARIABLE if secret == CI_OAUTH_SECRET else CI_OAUTH_POOL_VARIABLE
        receipt_index = next(i for i, call in enumerate(gh.calls) if call[:3] == ["variable", "set", receipt])
        assert gh.calls[index] == ["secret", "set", secret, "--repo", "souliane/teatree"]
        assert gh.stdin[index] == SECRET_TOKEN
        assert receipt_index > index

    @pytest.mark.parametrize(("secret", "write"), SECRET_WRITES)
    def test_the_installed_gh_accepts_the_argv(
        self, secret: str, write: Callable[[GhClient], None], tmp_path: Path
    ) -> None:
        gh_path = shutil.which("gh") or pytest.skip("gh is not installed")
        recorded: list[list[str]] = []

        def record(args: list[str], *, stdin_text: str | None = None) -> tuple[int, str]:
            recorded.append(args)
            return 0, ""

        write(record)
        argv = next(args for args in recorded if args[:2] == ["secret", "set"])
        assert argv[2] == secret
        clean_env = {"GH_CONFIG_DIR": str(tmp_path), "HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
        for args in recorded:
            result = subprocess.run(
                [gh_path, *args, "--help"], text=True, capture_output=True, env=clean_env, timeout=30, check=False
            )
            assert result.returncode == 0, (args, result.stderr)

        retired = subprocess.run(
            [gh_path, *argv, "--body-file", "-", "--help"],
            text=True,
            capture_output=True,
            env=clean_env,
            timeout=30,
            check=False,
        )
        assert retired.returncode != 0
        assert "unknown flag: --body-file" in retired.stderr

    @pytest.mark.parametrize("secret", [CI_OAUTH_SECRET, CI_OAUTH_POOL_SECRET])
    def test_failed_write_redacts_query_delimited_value(self, secret: str) -> None:
        token = "secret&tail"
        gh = FakeGh(fail_on=secret, fail_output=f"failed: token={token}")

        with pytest.raises(CiAccountSwitchError) as caught:
            _write_custom_token(secret, gh, token)

        assert str(caught.value).endswith(": failed: token=<redacted>")
        assert not gh.wrote_variable

    @pytest.mark.parametrize("secret", [CI_OAUTH_SECRET, CI_OAUTH_POOL_SECRET])
    def test_failed_write_sanitizes_other_credentials(self, secret: str) -> None:
        gh = FakeGh(fail_on=secret, fail_output="GH_TOKEN=value")

        with pytest.raises(CiAccountSwitchError) as caught:
            _write_custom_token(secret, gh, "separate-secret")

        assert str(caught.value).endswith(": GH_TOKEN=<redacted>")

    @pytest.mark.parametrize("secret", [CI_OAUTH_SECRET, CI_OAUTH_POOL_SECRET])
    def test_failed_write_limits_error_detail_to_500_characters(self, secret: str) -> None:
        gh = FakeGh(fail_on=secret, fail_output="x" * 2000)

        with pytest.raises(CiAccountSwitchError) as caught:
            _write_custom_token(secret, gh, "separate-secret")

        detail = str(caught.value).partition("(exit 1): ")[2]
        assert detail == "x" * 500

    @pytest.mark.parametrize("secret", [CI_OAUTH_SECRET, CI_OAUTH_POOL_SECRET])
    def test_failed_write_replaces_value_before_cutting_error(self, secret: str) -> None:
        token = "secret&tail"
        gh = FakeGh(fail_on=secret, fail_output="x" * 497 + token)

        with pytest.raises(CiAccountSwitchError) as caught:
            _write_custom_token(secret, gh, token)

        detail = str(caught.value).partition("(exit 1): ")[2]
        assert detail == "x" * 497 + "[RE"


class TestEmptyAllowlistFailsClosed(TestCase):
    def test_shipped_default_refuses_both_writes_without_calling_gh(self) -> None:
        allowlist = get_effective_settings().eval_ci_account_allowlist
        assert allowlist == []
        gh = FakeGh(active=SPENT_ACCOUNT)

        with pytest.raises(NoEligibleAccountError, match="allowlist"):
            _switcher(gh).reconcile_pool(_live_rows(), allowlist=allowlist)
        with pytest.raises(NoEligibleAccountError, match="allowlist"):
            _switcher(gh).switch(_live_rows(), run_start=RUN_START, allowlist=allowlist)
        assert gh.calls == []


class TestSelectionRule(TestCase):
    """The documented rule: exhausted is ineligible, the rest rank on run-start headroom."""

    def test_all_healthy_picks_the_most_headroom(self) -> None:
        rows = [_row(SOULIANE, u5h=0.46, u7d=0.82), _row(SPARE_ACCOUNT, u5h=0.10, u7d=0.13)]

        selection = select_account(rows, run_start=RUN_START)

        assert selection.best is not None
        assert selection.best.account == SPARE_ACCOUNT
        assert selection.rejected == ()

    def test_the_binding_window_decides_not_the_richer_one(self) -> None:
        """The spare account is weekly-richest but 5h-starved, so it loses a run starting NOW."""
        rows = _live_rows()

        selection = select_account(rows, run_start=RUN_START)

        assert selection.best is not None
        assert selection.best.account == SOULIANE
        assert selection.best.binding_headroom == pytest.approx(0.18)
        ranked = {entry.account: entry for entry in selection.ranked}
        assert ranked[SPARE_ACCOUNT].weighted_headroom > ranked[SOULIANE].weighted_headroom
        assert ranked[SPARE_ACCOUNT].binding_headroom < ranked[SOULIANE].binding_headroom

    def test_the_weighted_blend_breaks_ties_favouring_weekly_headroom(self) -> None:
        assert WEIGHT_7D > WEIGHT_5H
        assert pytest.approx(1.0) == WEIGHT_5H + WEIGHT_7D
        # Both bind at 0.30; the weekly-richer one must win the tie-break.
        five_hour_rich = _row(SOULIANE, u5h=0.10, u7d=0.70)
        weekly_rich = _row(SPARE_ACCOUNT, u5h=0.70, u7d=0.10)

        selection = select_account([five_hour_rich, weekly_rich], run_start=RUN_START)

        assert selection.best is not None
        assert selection.best.binding_headroom == pytest.approx(0.30)
        assert selection.best.account == SPARE_ACCOUNT

    def test_a_5h_window_resetting_before_the_run_counts_as_free(self) -> None:
        """The directive's case: 87 % 5h / 13 % weekly is a GOOD pick once its 5h has reset."""
        rows = _live_rows()
        later = RUN_START + dt.timedelta(hours=3)

        selection = select_account(rows, run_start=later)

        assert selection.best is not None
        assert selection.best.account == SPARE_ACCOUNT
        assert selection.best.headroom_5h == pytest.approx(1.0)
        assert selection.best.resets_before_run is True

    def test_one_exhausted_one_healthy_picks_the_healthy_one(self) -> None:
        rows = [_row(SPENT_ACCOUNT, u5h=0.0, u7d=1.0, status=TokenStatus.EXHAUSTED), _row(SOULIANE, u5h=0.46, u7d=0.82)]

        selection = select_account(rows, run_start=RUN_START)

        assert selection.best is not None
        assert selection.best.account == SOULIANE
        assert [rejection.account for rejection in selection.rejected] == [SPENT_ACCOUNT]

    def test_unreachable_and_missing_accounts_are_rejected_with_their_reason(self) -> None:
        rows = [
            _row(SOULIANE, u5h=0.0, u7d=0.0, status=TokenStatus.MISSING),
            _row(SPARE_ACCOUNT, u5h=0.0, u7d=0.0, status=TokenStatus.UNREACHABLE),
        ]

        selection = select_account(rows, run_start=RUN_START)

        assert selection.best is None
        reasons = {rejection.account: rejection.reason for rejection in selection.rejected}
        assert "pass" in reasons[SOULIANE]
        assert "probe" in reasons[SPARE_ACCOUNT]

    def test_metered_api_key_rows_are_not_candidates(self) -> None:
        """An API key cannot fill an OAuth secret — it is filtered, not rejected."""
        oauth = _row(SOULIANE, u5h=0.1, u7d=0.1)
        api_key = TokenAccountRow(
            account="anthropic/metered/api-key",
            kind=TokenKind.API_KEY,
            source=TokenSource.STORE,
            scopes=(),
            organization_id="org-test",
            utilization_5h=0.0,
            utilization_7d=0.0,
            weekly_reset=None,
            status=TokenStatus.HEALTHY,
        )

        selection = select_account([oauth, api_key], run_start=RUN_START)

        assert selection.best is not None
        assert selection.best.account == SOULIANE
        assert selection.rejected == ()


class TestSwitch(TestCase):
    """Applying the selection to the repo secret + its readable account variable."""

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("eval_ci_account_allowlist", [SOULIANE, SPARE_ACCOUNT, SPENT_ACCOUNT])

    def test_manual_switch_uses_only_allowlisted_accounts(self) -> None:
        ConfigSetting.objects.set_value("eval_ci_account_allowlist", [SPARE_ACCOUNT])
        gh = FakeGh(active=SPENT_ACCOUNT)
        outcome = _switcher(gh).switch(
            _live_rows(), run_start=RUN_START, allowlist=get_effective_settings().eval_ci_account_allowlist
        )
        assert outcome.account == SPARE_ACCOUNT
        assert gh.wrote_secret

    def test_manual_switch_refuses_an_unset_allowlist(self) -> None:
        ConfigSetting.objects.set_value("eval_ci_account_allowlist", [])
        gh = FakeGh(active=SPENT_ACCOUNT)
        with pytest.raises(NoEligibleAccountError, match="allowlist"):
            _switcher(gh).switch(
                _live_rows(), run_start=RUN_START, allowlist=get_effective_settings().eval_ci_account_allowlist
            )
        assert not gh.wrote_secret

    def test_switch_writes_the_secret_and_records_the_account(self) -> None:
        gh = FakeGh(active=SPENT_ACCOUNT)
        switcher = _switcher(gh)

        outcome = switcher.switch(
            _live_rows(), run_start=RUN_START, allowlist=get_effective_settings().eval_ci_account_allowlist
        )

        assert outcome.changed is True
        assert outcome.applied is True
        assert outcome.account == SOULIANE
        assert outcome.previous == SPENT_ACCOUNT
        assert gh.wrote_secret
        assert gh.wrote_variable

    def test_the_token_travels_on_stdin_never_in_argv(self) -> None:
        gh = FakeGh()
        switcher = _switcher(gh)

        switcher.switch(_live_rows(), run_start=RUN_START, allowlist=get_effective_settings().eval_ci_account_allowlist)

        flat_argv = " ".join(part for call in gh.calls for part in call)
        assert SECRET_TOKEN not in flat_argv
        assert SECRET_TOKEN in gh.stdin

    def test_already_optimal_is_a_no_op(self) -> None:
        gh = FakeGh(active=SOULIANE)
        switcher = _switcher(gh)

        outcome = switcher.switch(
            _live_rows(), run_start=RUN_START, allowlist=get_effective_settings().eval_ci_account_allowlist
        )

        assert outcome.changed is False
        assert outcome.applied is False
        assert outcome.account == SOULIANE
        assert not gh.wrote_secret
        assert not gh.wrote_variable

    def test_dry_run_reports_without_writing(self) -> None:
        gh = FakeGh(active=SPENT_ACCOUNT)
        switcher = _switcher(gh)

        outcome = switcher.switch(
            _live_rows(),
            run_start=RUN_START,
            allowlist=get_effective_settings().eval_ci_account_allowlist,
            dry_run=True,
        )

        assert outcome.changed is True
        assert outcome.applied is False
        assert outcome.account == SOULIANE
        assert not gh.wrote_secret

    def test_all_exhausted_fails_loud_and_writes_nothing(self) -> None:
        gh = FakeGh(active=SPENT_ACCOUNT)
        switcher = _switcher(gh)
        rows = [
            _row(SOULIANE, u5h=0.99, u7d=0.5, status=TokenStatus.EXHAUSTED),
            _row(SPARE_ACCOUNT, u5h=0.2, u7d=1.0, status=TokenStatus.EXHAUSTED),
            _row(SPENT_ACCOUNT, u5h=0.0, u7d=0.0, status=TokenStatus.MISSING),
        ]

        with pytest.raises(NoEligibleAccountError) as caught:
            switcher.switch(rows, run_start=RUN_START, allowlist=get_effective_settings().eval_ci_account_allowlist)

        message = str(caught.value)
        for account in (SOULIANE, SPARE_ACCOUNT, SPENT_ACCOUNT):
            assert account in message
        assert message.count("exhausted") >= 2
        assert not gh.wrote_secret
        assert not gh.wrote_variable

    def test_no_oauth_accounts_configured_fails_loud(self) -> None:
        gh = FakeGh()
        switcher = _switcher(gh)

        with pytest.raises(NoEligibleAccountError) as caught:
            switcher.switch([], run_start=RUN_START, allowlist=get_effective_settings().eval_ci_account_allowlist)

        assert "anthropic_oauth_pass_paths" in str(caught.value)
        assert not gh.wrote_secret

    def test_a_failed_secret_write_never_echoes_the_token(self) -> None:
        gh = FakeGh(active=SPENT_ACCOUNT, fail_on=CI_OAUTH_SECRET, fail_output=f"permission denied: {SECRET_TOKEN}")
        switcher = _switcher(gh)

        with pytest.raises(RuntimeError) as caught:
            switcher.switch(
                _live_rows(), run_start=RUN_START, allowlist=get_effective_settings().eval_ci_account_allowlist
            )

        assert SECRET_TOKEN not in str(caught.value)
        assert "permission denied" in str(caught.value)
        assert "[REDACTED]" in str(caught.value)
        assert CI_OAUTH_SECRET in str(caught.value)
        assert not gh.wrote_variable

    def test_an_empty_pass_entry_fails_before_touching_the_secret(self) -> None:
        gh = FakeGh(active=SPENT_ACCOUNT)
        switcher = CiAccountSwitcher(repo="souliane/teatree", gh=gh, secret_reader=lambda _account: "")

        with pytest.raises(RuntimeError):
            switcher.switch(
                _live_rows(), run_start=RUN_START, allowlist=get_effective_settings().eval_ci_account_allowlist
            )

        assert not gh.wrote_secret

    def test_newline_bearing_token_refuses_before_switch_write(self) -> None:
        for token in ("first\nsecond", "first\rsecond"):
            with self.subTest(token=repr(token)):
                gh = FakeGh(active=SPENT_ACCOUNT)
                switcher = CiAccountSwitcher(
                    repo="souliane/teatree", gh=gh, secret_reader={SOULIANE: token}.__getitem__
                )

                with pytest.raises(CiAccountSwitchError, match="newline"):
                    switcher.switch(
                        _live_rows(), run_start=RUN_START, allowlist=get_effective_settings().eval_ci_account_allowlist
                    )

                assert not gh.wrote_secret
                assert not gh.wrote_variable

    def test_active_account_reads_the_readable_companion_variable(self) -> None:
        gh = FakeGh(active=SPARE_ACCOUNT)
        switcher = _switcher(gh)

        assert switcher.active_account() == SPARE_ACCOUNT
        assert any(CI_ACCOUNT_VARIABLE in " ".join(call) for call in gh.calls)

    def test_an_unset_variable_reads_as_no_active_account(self) -> None:
        switcher = _switcher(FakeGh(active=""))

        assert switcher.active_account() == ""

    def test_default_client_refuses_ambient_login_when_route_is_unset(self) -> None:
        calls: list[object] = []
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("GH_TOKEN", "hostile")
            monkeypatch.setattr(
                "teatree.ci_oauth_switch.resolve_slug_token",
                lambda *_args, **_kwargs: ForgeTokenResolution("github_token", "owner", ForgeTokenState.UNSET),
                raising=False,
            )
            monkeypatch.setattr(
                "teatree.ci_oauth_switch.run_allowed_to_fail",
                lambda *args, **kwargs: calls.append((args, kwargs)),
            )
            switcher = CiAccountSwitcher(repo="souliane/teatree")

            assert switcher.active_account() == ""
        assert calls == []

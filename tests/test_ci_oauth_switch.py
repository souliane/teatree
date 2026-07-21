"""Tests for the CI OAuth account auto-switch (``teatree.ci_oauth_switch``).

The switcher reads the SAME rows ``t3 tokens`` renders, ranks the eligible accounts on
headroom as of the run's start, and points the ``CLAUDE_CODE_OAUTH_TOKEN`` repo secret at
the winner. These tests drive real :class:`~teatree.token_report.TokenReport` rows built
from canned health (no network, no ``pass``) through a recording fake ``gh``, and assert
the selection rule, the loud all-exhausted refusal, the already-optimal no-op, and — the
load-bearing invariant — that a token value never reaches any captured output.
"""

import datetime as dt

import pytest
from django.test import TestCase

from teatree.ci_oauth_switch import (
    CI_ACCOUNT_VARIABLE,
    CI_OAUTH_SECRET,
    WEIGHT_5H,
    WEIGHT_7D,
    CiAccountSwitcher,
    NoEligibleAccountError,
    select_account,
)
from teatree.credential_config import TokenKind
from teatree.token_report import TokenAccountRow, TokenSource, TokenStatus

RUN_START = dt.datetime(2026, 7, 21, 12, 0, tzinfo=dt.UTC)
SECRET_TOKEN = "sk-" + "ant-oat01-never-printed-anywhere"

SOULIANE = "anthropic/primary@example.com/oauth-token"
AGENTICALLY = "anthropic/spare@example.com/oauth-token"
JOHNJOHN = "anthropic/spent@example.com/oauth-token"


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
        _row(AGENTICALLY, u5h=0.87, u7d=0.13, status=TokenStatus.WARNING),
        _row(JOHNJOHN, u5h=0.0, u7d=1.0, status=TokenStatus.EXHAUSTED),
    ]


class FakeGh:
    """A recording ``gh`` stand-in: canned variable value, captured argv + stdin."""

    def __init__(self, *, active: str = "", fail_on: str = "") -> None:
        self.active = active
        self.fail_on = fail_on
        self.calls: list[list[str]] = []
        self.stdin: list[str | None] = []

    def __call__(self, args: list[str], *, stdin_text: str | None = None) -> tuple[int, str]:
        self.calls.append(list(args))
        self.stdin.append(stdin_text)
        if self.fail_on and self.fail_on in args:
            return 1, "boom"
        if args[0] == "api":
            return (0, self.active) if self.active else (1, "not found")
        return 0, ""

    @property
    def wrote_secret(self) -> bool:
        return any(call[:2] == ["secret", "set"] for call in self.calls)

    @property
    def wrote_variable(self) -> bool:
        return any(call[:2] == ["variable", "set"] for call in self.calls)


def _switcher(gh: FakeGh) -> CiAccountSwitcher:
    return CiAccountSwitcher(repo="souliane/teatree", gh=gh, secret_reader=lambda _account: SECRET_TOKEN)


class TestSelectionRule(TestCase):
    """The documented rule: exhausted is ineligible, the rest rank on run-start headroom."""

    def test_all_healthy_picks_the_most_headroom(self) -> None:
        rows = [_row(SOULIANE, u5h=0.46, u7d=0.82), _row(AGENTICALLY, u5h=0.10, u7d=0.13)]

        selection = select_account(rows, run_start=RUN_START)

        assert selection.best is not None
        assert selection.best.account == AGENTICALLY
        assert selection.rejected == ()

    def test_the_binding_window_decides_not_the_richer_one(self) -> None:
        """Agentically is weekly-richest but 5h-starved, so it loses a run starting NOW."""
        rows = _live_rows()

        selection = select_account(rows, run_start=RUN_START)

        assert selection.best is not None
        assert selection.best.account == SOULIANE
        assert selection.best.binding_headroom == pytest.approx(0.18)
        ranked = {entry.account: entry for entry in selection.ranked}
        assert ranked[AGENTICALLY].weighted_headroom > ranked[SOULIANE].weighted_headroom
        assert ranked[AGENTICALLY].binding_headroom < ranked[SOULIANE].binding_headroom

    def test_the_weighted_blend_breaks_ties_favouring_weekly_headroom(self) -> None:
        assert WEIGHT_7D > WEIGHT_5H
        assert pytest.approx(1.0) == WEIGHT_5H + WEIGHT_7D
        # Both bind at 0.30; the weekly-richer one must win the tie-break.
        five_hour_rich = _row(SOULIANE, u5h=0.10, u7d=0.70)
        weekly_rich = _row(AGENTICALLY, u5h=0.70, u7d=0.10)

        selection = select_account([five_hour_rich, weekly_rich], run_start=RUN_START)

        assert selection.best is not None
        assert selection.best.binding_headroom == pytest.approx(0.30)
        assert selection.best.account == AGENTICALLY

    def test_a_5h_window_resetting_before_the_run_counts_as_free(self) -> None:
        """The directive's case: 87 % 5h / 13 % weekly is a GOOD pick once its 5h has reset."""
        rows = _live_rows()
        later = RUN_START + dt.timedelta(hours=3)

        selection = select_account(rows, run_start=later)

        assert selection.best is not None
        assert selection.best.account == AGENTICALLY
        assert selection.best.headroom_5h == pytest.approx(1.0)
        assert selection.best.resets_before_run is True

    def test_one_exhausted_one_healthy_picks_the_healthy_one(self) -> None:
        rows = [_row(JOHNJOHN, u5h=0.0, u7d=1.0, status=TokenStatus.EXHAUSTED), _row(SOULIANE, u5h=0.46, u7d=0.82)]

        selection = select_account(rows, run_start=RUN_START)

        assert selection.best is not None
        assert selection.best.account == SOULIANE
        assert [rejection.account for rejection in selection.rejected] == [JOHNJOHN]

    def test_unreachable_and_missing_accounts_are_rejected_with_their_reason(self) -> None:
        rows = [
            _row(SOULIANE, u5h=0.0, u7d=0.0, status=TokenStatus.MISSING),
            _row(AGENTICALLY, u5h=0.0, u7d=0.0, status=TokenStatus.UNREACHABLE),
        ]

        selection = select_account(rows, run_start=RUN_START)

        assert selection.best is None
        reasons = {rejection.account: rejection.reason for rejection in selection.rejected}
        assert "pass" in reasons[SOULIANE]
        assert "probe" in reasons[AGENTICALLY]

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

    def test_switch_writes_the_secret_and_records_the_account(self) -> None:
        gh = FakeGh(active=JOHNJOHN)
        switcher = _switcher(gh)

        outcome = switcher.switch(_live_rows(), run_start=RUN_START)

        assert outcome.changed is True
        assert outcome.applied is True
        assert outcome.account == SOULIANE
        assert outcome.previous == JOHNJOHN
        assert gh.wrote_secret
        assert gh.wrote_variable

    def test_the_token_travels_on_stdin_never_in_argv(self) -> None:
        gh = FakeGh()
        switcher = _switcher(gh)

        switcher.switch(_live_rows(), run_start=RUN_START)

        flat_argv = " ".join(part for call in gh.calls for part in call)
        assert SECRET_TOKEN not in flat_argv
        assert SECRET_TOKEN in gh.stdin

    def test_already_optimal_is_a_no_op(self) -> None:
        gh = FakeGh(active=SOULIANE)
        switcher = _switcher(gh)

        outcome = switcher.switch(_live_rows(), run_start=RUN_START)

        assert outcome.changed is False
        assert outcome.applied is False
        assert outcome.account == SOULIANE
        assert not gh.wrote_secret
        assert not gh.wrote_variable

    def test_dry_run_reports_without_writing(self) -> None:
        gh = FakeGh(active=JOHNJOHN)
        switcher = _switcher(gh)

        outcome = switcher.switch(_live_rows(), run_start=RUN_START, dry_run=True)

        assert outcome.changed is True
        assert outcome.applied is False
        assert outcome.account == SOULIANE
        assert not gh.wrote_secret

    def test_all_exhausted_fails_loud_and_writes_nothing(self) -> None:
        gh = FakeGh(active=JOHNJOHN)
        switcher = _switcher(gh)
        rows = [
            _row(SOULIANE, u5h=0.99, u7d=0.5, status=TokenStatus.EXHAUSTED),
            _row(AGENTICALLY, u5h=0.2, u7d=1.0, status=TokenStatus.EXHAUSTED),
            _row(JOHNJOHN, u5h=0.0, u7d=0.0, status=TokenStatus.MISSING),
        ]

        with pytest.raises(NoEligibleAccountError) as caught:
            switcher.switch(rows, run_start=RUN_START)

        message = str(caught.value)
        for account in (SOULIANE, AGENTICALLY, JOHNJOHN):
            assert account in message
        assert message.count("exhausted") >= 2
        assert not gh.wrote_secret
        assert not gh.wrote_variable

    def test_no_oauth_accounts_configured_fails_loud(self) -> None:
        gh = FakeGh()
        switcher = _switcher(gh)

        with pytest.raises(NoEligibleAccountError) as caught:
            switcher.switch([], run_start=RUN_START)

        assert "anthropic_oauth_pass_paths" in str(caught.value)
        assert not gh.wrote_secret

    def test_a_failed_secret_write_never_echoes_the_token(self) -> None:
        gh = FakeGh(active=JOHNJOHN, fail_on=CI_OAUTH_SECRET)
        switcher = _switcher(gh)

        with pytest.raises(RuntimeError) as caught:
            switcher.switch(_live_rows(), run_start=RUN_START)

        assert SECRET_TOKEN not in str(caught.value)
        assert CI_OAUTH_SECRET in str(caught.value)
        assert not gh.wrote_variable

    def test_an_empty_pass_entry_fails_before_touching_the_secret(self) -> None:
        gh = FakeGh(active=JOHNJOHN)
        switcher = CiAccountSwitcher(repo="souliane/teatree", gh=gh, secret_reader=lambda _account: "")

        with pytest.raises(RuntimeError):
            switcher.switch(_live_rows(), run_start=RUN_START)

        assert not gh.wrote_secret

    def test_active_account_reads_the_readable_companion_variable(self) -> None:
        gh = FakeGh(active=AGENTICALLY)
        switcher = _switcher(gh)

        assert switcher.active_account() == AGENTICALLY
        assert any(CI_ACCOUNT_VARIABLE in " ".join(call) for call in gh.calls)

    def test_an_unset_variable_reads_as_no_active_account(self) -> None:
        switcher = _switcher(FakeGh(active=""))

        assert switcher.active_account() == ""

"""Tests for ``t3 eval ci-account`` — the CI OAuth account switch surface.

Drives the real typer app with the account health rows and the switcher stubbed, so the
assertions are about the CLI contract: exit 1 (not 0) when no account can serve the run,
a no-op when the secret already points at the best account, and — the load-bearing
invariant — no token value in any captured output.
"""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.ci_oauth_switch import NoEligibleAccountError, Rejection, SwitchOutcome
from teatree.cli import app

SECRET_TOKEN = "sk-" + "ant-oat01-never-printed-anywhere"
HEALTHY = "anthropic/primary@example.com/oauth-token"
SPENT = "anthropic/spent@example.com/oauth-token"


def _outcome(*, changed: bool, applied: bool, previous: str = SPENT) -> SwitchOutcome:
    return SwitchOutcome(
        account=HEALTHY,
        previous=previous,
        changed=changed,
        applied=applied,
        binding_headroom=0.42,
        headroom_5h=0.54,
        headroom_7d=0.42,
        rejected=(Rejection(SPENT, "exhausted — 5h 0% used, weekly 100% used"),),
    )


@pytest.fixture
def stub_switcher():
    """Patch the CLI's row source and switcher so no ORM, ``pass``, or ``gh`` is touched."""
    with (
        patch("teatree.cli.eval.ci_account._rows", return_value=[]),
        patch("teatree.cli.eval.ci_account._switcher") as factory,
    ):
        yield factory.return_value


class TestSwitchCommand:
    def test_a_performed_switch_exits_zero_and_names_both_accounts(self, stub_switcher) -> None:
        stub_switcher.switch.return_value = _outcome(changed=True, applied=True)

        result = CliRunner().invoke(app, ["eval", "ci-account", "switch"])

        assert result.exit_code == 0
        assert HEALTHY in result.output
        assert SPENT in result.output

    def test_already_optimal_reports_a_no_op(self, stub_switcher) -> None:
        stub_switcher.switch.return_value = _outcome(changed=False, applied=False, previous=HEALTHY)

        result = CliRunner().invoke(app, ["eval", "ci-account", "switch"])

        assert result.exit_code == 0
        assert "no-op" in result.output

    def test_dry_run_is_forwarded_to_the_switcher(self, stub_switcher) -> None:
        stub_switcher.switch.return_value = _outcome(changed=True, applied=False)

        result = CliRunner().invoke(app, ["eval", "ci-account", "switch", "--dry-run"])

        assert result.exit_code == 0
        assert stub_switcher.switch.call_args.kwargs["dry_run"] is True
        assert "would switch" in result.output

    def test_no_eligible_account_exits_nonzero_naming_every_rejection(self, stub_switcher) -> None:
        stub_switcher.switch.side_effect = NoEligibleAccountError(
            f"no eligible Anthropic OAuth account:\n  {SPENT}: exhausted\n  {HEALTHY}: exhausted"
        )

        result = CliRunner().invoke(app, ["eval", "ci-account", "switch"])

        assert result.exit_code == 1
        assert SPENT in result.output
        assert HEALTHY in result.output

    def test_no_captured_output_ever_carries_a_token_value(self, stub_switcher) -> None:
        stub_switcher.switch.return_value = _outcome(changed=True, applied=True)
        stub_switcher.active_account.return_value = SPENT

        switched = CliRunner().invoke(app, ["eval", "ci-account", "switch", "--json"])
        shown = CliRunner().invoke(app, ["eval", "ci-account", "show", "--json"])

        assert SECRET_TOKEN not in switched.output
        assert SECRET_TOKEN not in shown.output
        assert ("sk-" + "ant-") not in switched.output
        assert ("sk-" + "ant-") not in shown.output


class TestShowCommand:
    def test_show_reports_the_account_the_secret_currently_holds(self, stub_switcher) -> None:
        stub_switcher.active_account.return_value = HEALTHY

        result = CliRunner().invoke(app, ["eval", "ci-account", "show"])

        assert result.exit_code == 0
        assert HEALTHY in result.output

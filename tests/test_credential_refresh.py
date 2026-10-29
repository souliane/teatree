"""OAuth refresh behavior at the credential dispatch boundary."""

import datetime as dt
from unittest.mock import Mock

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import AnthropicTokenUsage
from teatree.core.models.anthropic_token_usage import TokenHealthReading
from teatree.credential_refresh import AccountProber, refresh_oauth_readings
from teatree.llm.rate_limits import RateLimitProbeError, RateLimitSnapshot
from teatree.utils.secrets import SecretStoreError


def _reading() -> TokenHealthReading:
    return TokenHealthReading(
        organization_id="org",
        utilization_5h=0.1,
        utilization_7d=0.1,
        status_5h="allowed",
        status_7d="allowed",
        reset_5h=None,
        reset_7d=None,
    )


class TestRefreshOAuthReadings(TestCase):
    def test_refreshes_only_missing_or_stale_rows(self) -> None:
        now = timezone.now()
        AnthropicTokenUsage.objects.record("fresh", _reading(), now=now)
        AnthropicTokenUsage.objects.record("stale", _reading(), now=now - dt.timedelta(hours=1))
        prober = Mock(spec=AccountProber)
        prober.read.side_effect = [_reading(), None]

        refresh_oauth_readings(["fresh", "stale", "missing"], now=now, prober=prober)

        assert [call.args[0] for call in prober.read.call_args_list] == ["stale", "missing"]
        assert AnthropicTokenUsage.objects.get(pass_path="stale").checked_at == now
        assert not AnthropicTokenUsage.objects.filter(pass_path="missing").exists()


def test_account_prober_reads_and_parses_one_account() -> None:
    snapshot = RateLimitSnapshot(
        organization_id="org",
        unified_5h_status="allowed",
        unified_5h_utilization=0.1,
        unified_5h_reset=None,
        unified_7d_status="allowed",
        unified_7d_utilization=0.2,
        unified_7d_reset=None,
        retry_after=None,
    )
    reader = Mock(return_value=snapshot)
    prober = AccountProber(reader=reader, secret_reader=lambda path: f"token:{path}")

    assert prober.read("account") == TokenHealthReading(
        organization_id="org",
        utilization_5h=0.1,
        utilization_7d=0.2,
        status_5h="allowed",
        status_7d="allowed",
        reset_5h=None,
        reset_7d=None,
    )
    reader.assert_called_once_with("token:account", is_oauth=True)


def test_account_prober_preserves_unreadable_accounts_for_fail_open_selection() -> None:
    reader = Mock(side_effect=RateLimitProbeError("down"))
    assert AccountProber(reader=reader, secret_reader=lambda path: "token").read("account") is None
    assert AccountProber(reader=reader, secret_reader=lambda path: "").read("account") is None

    def unreadable(path: str) -> str:
        raise SecretStoreError(path)

    assert AccountProber(reader=reader, secret_reader=unreadable).read("account") is None
    reader.assert_called_once()

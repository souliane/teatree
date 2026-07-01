r"""The Anthropic per-account health cache (``teatree.core.models.anthropic_token_usage``).

The exhaustion rule and the ``valid_until`` policy are pure (no DB), so they are
parametrized in plain pytest classes; the ``record`` upsert and the row-level
freshness/reset accessors are DB-backed and use ``django.test.TestCase``.
"""

import datetime as dt

import pytest
from django.test import TestCase

from teatree.core.models import AnthropicTokenUsage
from teatree.core.models.anthropic_token_usage import HEALTH_TTL, TokenHealthReading

_NOW = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)


def _reading(
    *,
    u5: float = 0.0,
    u7: float = 0.0,
    s7: str = "allowed",
    reset_5h: dt.datetime | None = None,
    reset_7d: dt.datetime | None = None,
) -> TokenHealthReading:
    return TokenHealthReading(
        organization_id="org-1",
        utilization_5h=u5,
        utilization_7d=u7,
        status_5h="allowed",
        status_7d=s7,
        reset_5h=reset_5h,
        reset_7d=reset_7d,
    )


class TestExhaustionRule:
    @pytest.mark.parametrize(
        ("u5", "u7", "s7", "exhausted"),
        [
            (0.30, 0.80, "allowed", False),
            (0.95, 0.0, "allowed", True),
            (0.9499, 0.0, "allowed", False),
            (0.0, 0.99, "allowed", True),
            (0.0, 0.9899, "allowed", False),
            (0.0, 0.0, "rejected", True),
        ],
    )
    def test_exhaustion_thresholds(self, u5: float, u7: float, s7: str, *, exhausted: bool) -> None:
        assert _reading(u5=u5, u7=u7, s7=s7).is_exhausted is exhausted


class TestValidUntilPolicy:
    def test_healthy_token_expires_after_the_short_ttl(self) -> None:
        # Healthy with distant resets → re-probe after the TTL, not at the far reset.
        reading = _reading(reset_5h=_NOW + dt.timedelta(hours=3), reset_7d=_NOW + dt.timedelta(days=5))
        assert reading.valid_until(_NOW) == _NOW + HEALTH_TTL

    def test_healthy_token_never_outlives_a_nearer_reset(self) -> None:
        near = _NOW + dt.timedelta(minutes=2)
        assert _reading(reset_5h=near).valid_until(_NOW) == near

    def test_exhausted_token_is_valid_until_its_blocking_window_resets(self) -> None:
        # An exhausted 5h window → not re-probed until the 5h reset (far past the TTL).
        reset = _NOW + dt.timedelta(hours=2)
        assert _reading(u5=0.97, reset_5h=reset).valid_until(_NOW) == reset

    def test_two_exhausted_windows_wait_for_the_later_reset(self) -> None:
        reset_5h = _NOW + dt.timedelta(hours=2)
        reset_7d = _NOW + dt.timedelta(days=3)
        assert _reading(u5=0.97, u7=0.995, reset_5h=reset_5h, reset_7d=reset_7d).valid_until(_NOW) == reset_7d

    def test_exhausted_without_a_known_reset_falls_back_to_the_ttl(self) -> None:
        assert _reading(s7="rejected").valid_until(_NOW) == _NOW + HEALTH_TTL


class TestRecordUpsert(TestCase):
    def test_record_creates_a_row_with_the_computed_valid_until(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(u5=0.30, u7=0.80), now=_NOW)
        assert row.pass_path == "anthropic/acct/oauth"
        assert row.organization_id == "org-1"
        assert row.valid_until == _NOW + HEALTH_TTL
        assert not row.is_exhausted

    def test_record_is_idempotent_on_pass_path(self) -> None:
        AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(u5=0.10), now=_NOW)
        AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(u5=0.97), now=_NOW)
        rows = AnthropicTokenUsage.objects.filter(pass_path="anthropic/acct/oauth")
        assert rows.count() == 1
        assert rows.get().is_exhausted, "the re-probe overwrote the one row with the fresh verdict"


class TestRowHealthAccessors(TestCase):
    def test_is_fresh_tracks_valid_until(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(), now=_NOW)
        assert row.is_fresh(_NOW + dt.timedelta(minutes=1))
        assert not row.is_fresh(_NOW + dt.timedelta(minutes=10))

    def test_earliest_reset_is_the_soonest_window(self) -> None:
        reset_5h = _NOW + dt.timedelta(hours=2)
        reset_7d = _NOW + dt.timedelta(days=3)
        row = AnthropicTokenUsage.objects.record(
            "anthropic/acct/oauth", _reading(reset_5h=reset_5h, reset_7d=reset_7d), now=_NOW
        )
        assert row.earliest_reset == reset_5h

    def test_earliest_reset_is_none_when_no_window_is_known(self) -> None:
        row = AnthropicTokenUsage.objects.record("anthropic/acct/oauth", _reading(), now=_NOW)
        assert row.earliest_reset is None

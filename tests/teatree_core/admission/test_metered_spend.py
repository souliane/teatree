"""The metered lane's spend ledger and its ceiling (souliane/teatree#4816).

Four runs reached a provider cycle limit unbounded because nothing in the pressure
model measured metered spend at all. The ledger is what a ceiling can be keyed on —
in TOKENS, which are measured, never in the estimated USD, which is price-table
arithmetic whose error is unknown.
"""

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.admission.metered_spend import read_metered_spend
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket


def test_metered_signal_reader_is_owned_by_the_metered_ledger_module() -> None:
    from teatree.core import admission_governor  # noqa: PLC0415 — assert the compatibility re-export
    from teatree.core.admission import metered_spend  # noqa: PLC0415 — assert the public boundary

    assert admission_governor.read_metered_signal is metered_spend.read_metered_signal


_CEILING = 1_000_000


class MeteredSpendCase(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("metered_token_ceiling", str(_CEILING))

    def attempt(self, **overrides: object) -> TaskAttempt:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket, overlay="test"),
            phase="coding",
        )
        fields: dict[str, object] = {
            "task": task,
            "ended_at": timezone.now(),
            "lane": TaskAttempt.Lane.METERED,
            "input_tokens": 500_000,
            "output_tokens": 100_000,
            "cost_usd": 3.5,
            "cost_is_estimated": True,
        }
        return TaskAttempt.objects.create(**{**fields, **overrides})


class TestTheLedgerMeasuresTheMeteredLane(MeteredSpendCase):
    def test_spend_over_the_ceiling_reads_over_one(self) -> None:
        self.attempt()
        self.attempt()

        spend = read_metered_spend()

        assert spend.fresh is True
        assert spend.tokens == 1_200_000
        assert spend.utilization() > 1.0

    def test_the_detail_names_the_lane_the_ceiling_the_window_and_flags_the_estimate(self) -> None:
        self.attempt()

        detail = read_metered_spend().detail()

        assert "metered" in detail
        assert f"{_CEILING:,}" in detail
        assert "24h" in detail
        assert "ESTIMATED (price-table arithmetic, not a billed amount)" in detail

    def test_unknown_usage_attempts_make_the_figure_a_declared_floor(self) -> None:
        self.attempt()
        self.attempt(input_tokens=None, output_tokens=None, cost_usd=None, usage_unknown=True)

        spend = read_metered_spend()

        assert spend.unknown_attempts == 1
        assert "FLOOR" in spend.detail()
        assert "1 attempt" in spend.detail()

    def test_control_the_same_spend_on_the_subscription_lane_contributes_nothing(self) -> None:
        self.attempt(lane=TaskAttempt.Lane.SUBSCRIPTION)

        spend = read_metered_spend()

        assert spend.tokens == 0
        assert spend.utilization() == pytest.approx(0.0)

    def test_control_spend_older_than_the_window_contributes_nothing(self) -> None:
        self.attempt(ended_at=timezone.now() - timedelta(hours=25))

        assert read_metered_spend().tokens == 0

    def test_control_the_shipped_default_ceiling_can_never_brake_anything(self) -> None:
        ConfigSetting.objects.set_value("metered_token_ceiling", "0")
        self.attempt()
        self.attempt()

        spend = read_metered_spend()

        assert spend.ceiling == 0
        assert spend.tokens == 1_200_000
        assert spend.utilization() == pytest.approx(0.0)

    def test_control_a_ledger_read_that_raises_is_not_fresh_and_never_brakes(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415 — local to the one degraded-read case

        with patch("teatree.core.admission.metered_spend._window_totals", side_effect=RuntimeError("db gone")):
            spend = read_metered_spend()

        assert spend.fresh is False
        assert spend.utilization() == pytest.approx(0.0)

    def test_the_estimated_cost_is_reported_but_never_the_thing_the_ceiling_keys_on(self) -> None:
        self.attempt(input_tokens=_CEILING * 4, output_tokens=0, cost_usd=0.01)

        spend = read_metered_spend()

        assert spend.estimated_cost_usd == pytest.approx(0.01)
        assert spend.utilization() == pytest.approx(4.0), "the ceiling keys on tokens, not the estimate"

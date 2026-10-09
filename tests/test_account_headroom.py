"""The one headroom-ranking implementation (``teatree.account_headroom``).

A Django-free foundation leaf, so the live routing selector, the CI switcher and the
eval selector can all reach it. These tests pin the two scores, the reset projection,
and the ``order`` tie-break that lets ONE sort key serve all three callers.
"""

import datetime as dt

import pytest

from teatree.account_headroom import WEIGHT_5H, WEIGHT_7D, AccountHeadroom, headroom_at, rank

AT = dt.datetime(2026, 7, 21, 12, 0, tzinfo=dt.UTC)


def _entry(account: str, *, h5: float, h7: float, order: int = 0) -> AccountHeadroom:
    return AccountHeadroom(
        account=account,
        order=order,
        utilization_5h=1.0 - h5,
        utilization_7d=1.0 - h7,
        headroom_5h=h5,
        headroom_7d=h7,
        resets_before_run=False,
    )


class TestScores:
    def test_binding_headroom_disqualifies_a_rich_weekly_behind_a_spent_five_hour(self) -> None:
        five_hour_spent = _entry("a", h5=0.02, h7=0.90)
        balanced = _entry("b", h5=0.40, h7=0.40)

        assert rank([five_hour_spent, balanced])[0].account == "b"

    def test_the_weighted_blend_breaks_a_binding_tie_favouring_weekly(self) -> None:
        assert WEIGHT_7D > WEIGHT_5H
        assert pytest.approx(1.0) == WEIGHT_5H + WEIGHT_7D
        five_hour_rich = _entry("a", h5=0.90, h7=0.30)
        weekly_rich = _entry("b", h5=0.30, h7=0.90)

        ranked = rank([five_hour_rich, weekly_rich])

        assert ranked[0].binding_headroom == pytest.approx(0.30)
        assert ranked[0].account == "b"

    def test_a_window_resetting_before_the_instant_reads_fully_free(self) -> None:
        headroom, resets = headroom_at(0.87, AT - dt.timedelta(minutes=1), AT)

        assert headroom == pytest.approx(1.0)
        assert resets is True

    def test_an_unreported_window_reads_fully_free_without_a_reset(self) -> None:
        assert headroom_at(None, None, AT) == (1.0, False)

    def test_a_window_resetting_after_the_instant_is_scored_on_its_utilization(self) -> None:
        assert headroom_at(0.60, AT + dt.timedelta(hours=1), AT) == (pytest.approx(0.40), False)


class TestOrderTieBreak:
    def test_order_breaks_a_tie_before_the_account_name(self) -> None:
        # Equal headroom: the caller's declared position wins, NOT the lexical name.
        late_but_first = _entry("z", h5=0.5, h7=0.5, order=0)
        early_but_second = _entry("a", h5=0.5, h7=0.5, order=1)

        assert [entry.account for entry in rank([early_but_second, late_but_first])] == ["z", "a"]

    def test_a_uniform_order_reduces_the_key_to_headroom_then_account_name(self) -> None:
        # What ``ci_oauth_switch`` relies on: at order=0 the key IS its historical one.
        first = _entry("b", h5=0.5, h7=0.5)
        second = _entry("a", h5=0.5, h7=0.5)

        assert [entry.account for entry in rank([first, second])] == ["a", "b"]

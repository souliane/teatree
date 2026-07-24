r"""The freshest-eval-OAuth selector (``teatree.eval.oauth_selection``).

Network-free: every test drives a mock probe mapping a token to a canned
:class:`~teatree.llm.rate_limits.RateLimitSnapshot`, so the ranking, the exhaustion
classification, the reset projection, and the load-bearing invariant (a token value
never reaches a snapshot, a score, or the returned selection) are all asserted without a
real ``/v1/messages`` call. Mock tokens are synthetic — no real credential appears.
"""

import datetime as dt

import pytest

from teatree.ci_oauth_switch import WEIGHT_5H as SWITCH_WEIGHT_5H
from teatree.ci_oauth_switch import WEIGHT_7D as SWITCH_WEIGHT_7D
from teatree.core.models.anthropic_token_usage import REJECTED_STATUS as MODEL_REJECTED_STATUS
from teatree.core.models.anthropic_token_usage import UTILIZATION_5H_LIMIT as MODEL_5H_LIMIT
from teatree.core.models.anthropic_token_usage import UTILIZATION_7D_LIMIT as MODEL_7D_LIMIT
from teatree.eval.oauth_selection import (
    REJECTED_STATUS,
    UTILIZATION_5H_LIMIT,
    UTILIZATION_7D_LIMIT,
    WEIGHT_5H,
    WEIGHT_7D,
    CandidateHealth,
    OAuthSelection,
    TokenProbeStatus,
    parse_tokens,
    select_freshest,
)
from teatree.llm.rate_limits import RateLimitProbeError, RateLimitSnapshot

RUN_START = dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.UTC)

# Synthetic mock tokens — deliberately NOT credential-shaped (no `sk-ant-*` prefix), so
# the probe key the mock reader maps on carries no resemblance to a real token.
FRESH = "mock-oauth-fresh"
MIDDLING = "mock-oauth-middling"
SPENT_5H = "mock-oauth-spent-5h"
REJECTED = "mock-oauth-rejected"
UNREACHABLE = "mock-oauth-unreachable"


def _snapshot(*, u5h: float, u7d: float, status_7d: str = "allowed", org: str = "org-mock") -> RateLimitSnapshot:
    """A canned OAuth snapshot; both windows reset AFTER RUN_START unless a test overrides."""
    return RateLimitSnapshot(
        organization_id=org,
        unified_5h_status="allowed",
        unified_5h_utilization=u5h,
        unified_5h_reset=RUN_START + dt.timedelta(hours=2),
        unified_7d_status=status_7d,
        unified_7d_utilization=u7d,
        unified_7d_reset=RUN_START + dt.timedelta(days=3),
        retry_after=None,
    )


class _MockReader:
    """A ``RateLimitReader`` mapping a token to a canned snapshot (or a probe error)."""

    def __init__(self, snapshots: dict[str, RateLimitSnapshot], *, errors: frozenset[str] = frozenset()) -> None:
        self._snapshots = snapshots
        self._errors = errors
        self.signed: list[str] = []

    def __call__(self, token: str, *, is_oauth: bool) -> RateLimitSnapshot:
        self.signed.append(token)
        assert is_oauth is True
        if token in self._errors:
            msg = "rate-limit probe transport failed: ConnectError"
            raise RateLimitProbeError(msg)
        return self._snapshots[token]


class TestParseTokens:
    def test_splits_strips_and_drops_blanks(self) -> None:
        assert parse_tokens("  a \n\n b\n") == ["a", "b"]

    def test_dedupes_preserving_first_seen_order(self) -> None:
        assert parse_tokens("b\na\nb\n") == ["b", "a"]

    def test_empty_blob_is_no_tokens(self) -> None:
        assert parse_tokens("") == []
        assert parse_tokens("\n  \n") == []


class TestSelectFreshest:
    def test_picks_the_account_with_the_most_binding_headroom(self) -> None:
        reader = _MockReader(
            {
                MIDDLING: _snapshot(u5h=0.50, u7d=0.50),
                FRESH: _snapshot(u5h=0.10, u7d=0.10),
            }
        )
        selection = select_freshest([MIDDLING, FRESH], reader=reader, now=RUN_START)
        winner = selection.winner
        assert winner is not None
        assert winner.label == "token[2]"
        assert winner.index == 1

    def test_binding_headroom_beats_a_richer_other_window(self) -> None:
        # SPENT_5H has a huge weekly balance but an almost-spent 5h window: the binding
        # (minimum) headroom must rank it below the evenly-fresh account.
        reader = _MockReader(
            {
                SPENT_5H: _snapshot(u5h=0.90, u7d=0.05),
                FRESH: _snapshot(u5h=0.40, u7d=0.40),
            }
        )
        selection = select_freshest([SPENT_5H, FRESH], reader=reader, now=RUN_START)
        assert selection.winner is not None
        assert selection.winner.label == "token[2]"

    def test_exhausted_account_is_ineligible_and_scores_out(self) -> None:
        reader = _MockReader(
            {
                REJECTED: _snapshot(u5h=0.10, u7d=0.10, status_7d=REJECTED_STATUS),
                FRESH: _snapshot(u5h=0.30, u7d=0.30),
            }
        )
        selection = select_freshest([REJECTED, FRESH], reader=reader, now=RUN_START)
        assert selection.winner is not None
        assert selection.winner.label == "token[2]"
        exhausted = selection.candidates[0]
        assert exhausted.status is TokenProbeStatus.EXHAUSTED
        assert exhausted.is_eligible is False

    def test_utilization_over_the_limit_is_exhausted(self) -> None:
        reader = _MockReader({SPENT_5H: _snapshot(u5h=0.96, u7d=0.10)})
        selection = select_freshest([SPENT_5H], reader=reader, now=RUN_START)
        assert selection.winner is None
        assert selection.all_ineligible is True
        assert selection.candidates[0].status is TokenProbeStatus.EXHAUSTED

    def test_all_exhausted_yields_no_winner(self) -> None:
        reader = _MockReader(
            {
                REJECTED: _snapshot(u5h=0.10, u7d=0.10, status_7d=REJECTED_STATUS),
                SPENT_5H: _snapshot(u5h=0.99, u7d=0.10),
            }
        )
        selection = select_freshest([REJECTED, SPENT_5H], reader=reader, now=RUN_START)
        assert selection.winner is None
        assert selection.all_ineligible is True

    def test_probe_error_is_unreachable_not_a_crash(self) -> None:
        reader = _MockReader({FRESH: _snapshot(u5h=0.10, u7d=0.10)}, errors={UNREACHABLE})
        selection = select_freshest([UNREACHABLE, FRESH], reader=reader, now=RUN_START)
        assert selection.winner is not None
        assert selection.winner.label == "token[2]"
        unreachable = selection.candidates[0]
        assert unreachable.status is TokenProbeStatus.UNREACHABLE
        assert unreachable.is_eligible is False

    def test_exact_tie_breaks_on_input_position_deterministic_first(self) -> None:
        identical = _snapshot(u5h=0.20, u7d=0.20)
        reader = _MockReader({FRESH: identical, MIDDLING: identical})
        selection = select_freshest([FRESH, MIDDLING], reader=reader, now=RUN_START)
        assert selection.winner is not None
        assert selection.winner.index == 0

    def test_five_hour_reset_before_run_start_counts_as_fully_free(self) -> None:
        reset_before = RateLimitSnapshot(
            organization_id="org-mock",
            unified_5h_status="allowed",
            unified_5h_utilization=0.90,  # spent NOW …
            unified_5h_reset=RUN_START - dt.timedelta(minutes=1),  # … but resets before the run
            unified_7d_status="allowed",
            unified_7d_utilization=0.20,
            unified_7d_reset=RUN_START + dt.timedelta(days=3),
            retry_after=None,
        )
        reader = _MockReader({SPENT_5H: reset_before, FRESH: _snapshot(u5h=0.50, u7d=0.50)})
        selection = select_freshest([SPENT_5H, FRESH], reader=reader, now=RUN_START)
        # SPENT_5H projects to 5h=1.0 / 7d=0.80 → binding 0.80, beating FRESH's 0.50.
        assert selection.winner is not None
        assert selection.winner.label == "token[1]"

    def test_empty_token_list_has_no_winner_and_is_not_all_ineligible(self) -> None:
        selection = select_freshest([], reader=_MockReader({}), now=RUN_START)
        assert selection.winner is None
        assert selection.all_ineligible is False
        assert selection.candidates == ()


class TestNoTokenLeak:
    def test_no_token_value_appears_in_the_selection(self) -> None:
        reader = _MockReader(
            {
                FRESH: _snapshot(u5h=0.10, u7d=0.10),
                REJECTED: _snapshot(u5h=0.10, u7d=0.10, status_7d=REJECTED_STATUS),
            }
        )
        selection = select_freshest([FRESH, REJECTED], reader=reader, now=RUN_START)
        blob = repr(selection)
        for token in (FRESH, REJECTED):
            assert token not in blob
        # The tokens WERE signed onto the probe (the only place they may appear).
        assert reader.signed == [FRESH, REJECTED]


class TestConstantParityWithCanonicalHomes:
    """The Django-free replicas must equal their canonical sources, or drift is a bug."""

    def test_exhaustion_thresholds_match_the_model(self) -> None:
        assert UTILIZATION_5H_LIMIT == MODEL_5H_LIMIT
        assert UTILIZATION_7D_LIMIT == MODEL_7D_LIMIT
        assert REJECTED_STATUS == MODEL_REJECTED_STATUS

    def test_tie_break_weights_match_ci_oauth_switch(self) -> None:
        assert WEIGHT_5H == SWITCH_WEIGHT_5H
        assert WEIGHT_7D == SWITCH_WEIGHT_7D


class TestCandidateHealthScoring:
    def test_binding_headroom_is_the_min_and_weighted_is_the_blend(self) -> None:
        candidate = CandidateHealth(
            index=0,
            label="token[1]",
            status=TokenProbeStatus.HEALTHY,
            headroom_5h=0.8,
            headroom_7d=0.2,
        )
        assert candidate.binding_headroom == pytest.approx(0.2)
        assert candidate.weighted_headroom == pytest.approx(WEIGHT_5H * 0.8 + WEIGHT_7D * 0.2)

    def test_default_selection_is_empty(self) -> None:
        assert OAuthSelection().winner is None
        assert OAuthSelection().all_ineligible is False

"""Unit tests for the pure staleness helpers (#2663 dream-batch dea750a552f8f2d2).

``factory_score_enabled`` cited its 2026-08-04 decision for 55+ days after the shipped
default should have followed it, because a citation only had to be PRESENT, never
recent. These pin the age arithmetic these helpers add so the same recurrence surfaces
as a fault instead of shielding a gate forever.
"""

import datetime as dt
from dataclasses import replace

from teatree.config.gate_evidence import (
    STAGED_STALE_AFTER_DAYS,
    ActivationIntent,
    GateEvidence,
    ObservableKind,
    staged_citation_is_stale,
    staged_decision_age_days,
)

TODAY = dt.date(2026, 9, 28)


def _staged(rationale: str) -> GateEvidence:
    return GateEvidence(
        setting="x_enabled",
        off_value=False,
        kind=ObservableKind.MODEL,
        target="core.X",
        shipped=dt.date(2026, 1, 1),
        intent=ActivationIntent.STAGED,
        rationale=rationale,
        satisfier="fixture satisfier",
    )


class TestStagedDecisionAgeDays:
    def test_an_iso_date_ages_from_today(self) -> None:
        assert staged_decision_age_days("souliane/teatree#4189 — decided 2026-08-04", TODAY) == 55

    def test_the_most_recent_of_several_dates_wins(self) -> None:
        rationale = "kept 2026-01-01, then re-affirmed 2026-08-04 (#4189)"
        assert staged_decision_age_days(rationale, TODAY) == 55

    def test_an_issue_only_citation_has_no_computable_age(self) -> None:
        assert staged_decision_age_days("souliane/teatree#117 — ships warn until a soak", TODAY) is None

    def test_no_citation_at_all_has_no_computable_age(self) -> None:
        assert staged_decision_age_days("no reference here", TODAY) is None


class TestStagedCitationIsStale:
    def test_a_dated_citation_past_the_threshold_is_stale(self) -> None:
        stale_date = TODAY - dt.timedelta(days=STAGED_STALE_AFTER_DAYS + 1)
        entry = _staged(f"souliane/teatree#4189 — decided {stale_date.isoformat()}")
        assert staged_citation_is_stale(entry, TODAY) is True

    def test_a_dated_citation_within_the_threshold_is_not_stale(self) -> None:
        fresh_date = TODAY - dt.timedelta(days=STAGED_STALE_AFTER_DAYS - 1)
        entry = _staged(f"souliane/teatree#4189 — decided {fresh_date.isoformat()}")
        assert staged_citation_is_stale(entry, TODAY) is False

    def test_an_issue_only_citation_is_never_stale(self) -> None:
        entry = _staged("souliane/teatree#117 — ships warn until a soak")
        assert staged_citation_is_stale(entry, TODAY) is False

    def test_an_undecided_entry_is_never_stale(self) -> None:
        """Staleness is a STAGED-only concept — UNDECIDED is already a fault on its own."""
        stale_date = TODAY - dt.timedelta(days=STAGED_STALE_AFTER_DAYS + 1)
        entry = replace(
            _staged(f"souliane/teatree#4189 — decided {stale_date.isoformat()}"),
            intent=ActivationIntent.UNDECIDED,
        )
        assert staged_citation_is_stale(entry, TODAY) is False

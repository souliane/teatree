"""The waiting-candidate ledger — the durable half of intake-starvation visibility (#4238)."""

import datetime as dt

from django.test import TestCase

from teatree.core.models import STARVED_AFTER, UnclaimedIntakeCandidate, WaitingCandidate

OVERLAY = "acme"
OLD = "https://github.com/souliane/teatree/issues/4188"
NEW = "https://github.com/souliane/teatree/issues/4234"
NOW = dt.datetime(2026, 8, 4, 12, tzinfo=dt.UTC)


class UnclaimedIntakeCandidateSyncTests(TestCase):
    """``sync`` replaces the overlay's waiting set and preserves each first sighting."""

    def test_sync_records_the_observed_set(self) -> None:
        UnclaimedIntakeCandidate.objects.sync(
            OVERLAY,
            [WaitingCandidate(issue_url=OLD, title="starved", issue_created_at=NOW - dt.timedelta(days=3))],
            now=NOW,
        )

        row = UnclaimedIntakeCandidate.objects.get(issue_url=OLD)
        assert row.title == "starved"
        assert row.first_seen_at == NOW
        assert row.issue_created_at == NOW - dt.timedelta(days=3)

    def test_a_re_observed_candidate_keeps_its_first_sighting(self) -> None:
        """The wait is measured from the FIRST sighting — an upsert must not reset the clock."""
        first = NOW - dt.timedelta(days=2)
        UnclaimedIntakeCandidate.objects.sync(OVERLAY, [WaitingCandidate(issue_url=OLD)], now=first)

        UnclaimedIntakeCandidate.objects.sync(OVERLAY, [WaitingCandidate(issue_url=OLD)], now=NOW)

        row = UnclaimedIntakeCandidate.objects.get(issue_url=OLD)
        assert row.first_seen_at == first
        assert row.last_seen_at == NOW

    def test_a_candidate_absent_from_the_pass_loses_its_row(self) -> None:
        UnclaimedIntakeCandidate.objects.sync(
            OVERLAY,
            [WaitingCandidate(issue_url=OLD), WaitingCandidate(issue_url=NEW)],
            now=NOW,
        )

        UnclaimedIntakeCandidate.objects.sync(OVERLAY, [WaitingCandidate(issue_url=NEW)], now=NOW)

        assert list(UnclaimedIntakeCandidate.objects.values_list("issue_url", flat=True)) == [NEW]

    def test_sync_leaves_another_overlays_rows_alone(self) -> None:
        UnclaimedIntakeCandidate.objects.sync("other", [WaitingCandidate(issue_url=OLD)], now=NOW)

        UnclaimedIntakeCandidate.objects.sync(OVERLAY, [], now=NOW)

        assert UnclaimedIntakeCandidate.objects.filter(overlay="other").exists()


class UnclaimedIntakeCandidateStarvedTests(TestCase):
    """``starved`` reads only the candidates that have waited past the threshold."""

    def test_a_fresh_candidate_is_not_starved(self) -> None:
        UnclaimedIntakeCandidate.objects.sync(OVERLAY, [WaitingCandidate(issue_url=NEW)], now=NOW)

        assert not UnclaimedIntakeCandidate.objects.starved(now=NOW).exists()

    def test_a_candidate_past_the_threshold_is_starved_longest_wait_first(self) -> None:
        UnclaimedIntakeCandidate.objects.sync(
            OVERLAY,
            [WaitingCandidate(issue_url=OLD), WaitingCandidate(issue_url=NEW)],
            now=NOW - STARVED_AFTER * 3,
        )
        UnclaimedIntakeCandidate.objects.filter(issue_url=NEW).update(first_seen_at=NOW - STARVED_AFTER * 2)

        starved = list(UnclaimedIntakeCandidate.objects.starved(now=NOW))

        assert [row.issue_url for row in starved] == [OLD, NEW]


class UnclaimedIntakeCandidateReportTests(TestCase):
    """The operator-facing line names the issue and both durations."""

    def test_report_names_the_wait_and_the_issue_age(self) -> None:
        UnclaimedIntakeCandidate.objects.sync(
            OVERLAY,
            [
                WaitingCandidate(
                    issue_url=OLD,
                    title="intake has no age ordering",
                    issue_created_at=NOW - dt.timedelta(days=5),
                ),
            ],
            now=NOW - dt.timedelta(days=2),
        )

        report = UnclaimedIntakeCandidate.objects.get(issue_url=OLD).report(now=NOW)

        assert OLD in report
        assert "intake has no age ordering" in report
        assert "unclaimed for 2d" in report
        assert "open 5d" in report

    def test_report_says_unknown_when_the_forge_gave_no_filing_date(self) -> None:
        UnclaimedIntakeCandidate.objects.sync(
            OVERLAY,
            [WaitingCandidate(issue_url=OLD)],
            now=NOW - dt.timedelta(hours=7),
        )

        report = UnclaimedIntakeCandidate.objects.get(issue_url=OLD).report(now=NOW)

        assert "unclaimed for 7h" in report
        assert "open unknown" in report

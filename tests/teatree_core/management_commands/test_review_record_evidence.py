"""``t3 <overlay> review record-evidence`` — record a PR-08 review-evidence artifact."""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ReviewEvidence, Ticket

_SHA = "a" * 40


class ReviewRecordEvidenceTest(TestCase):
    def test_records_cold_review_evidence(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        result = cast(
            "dict[str, object]",
            call_command(
                "review",
                "record-evidence",
                str(ticket.pk),
                "--kind",
                "cold_review",
                "--reviewer",
                "reviewer-bob",
                "--verdict",
                "merge_safe",
                "--head-sha",
                _SHA,
            ),
        )
        assert result["recorded"] is True
        assert ReviewEvidence.objects.has_cold_review(ticket) is True

    def test_records_integration_review_covering_repos(self) -> None:
        ticket = Ticket.objects.create(overlay="test", repos=["org/a", "org/b"])
        result = cast(
            "dict[str, object]",
            call_command(
                "review",
                "record-evidence",
                str(ticket.pk),
                "--kind",
                "integration_review",
                "--reviewer",
                "reviewer-bob",
                "--verdict",
                "pass",
                "--head-sha",
                _SHA,
                "--repos",
                "org/a,org/b",
            ),
        )
        assert result["recorded"] is True
        assert ReviewEvidence.objects.has_integration_review_covering(ticket, ["org/a", "org/b"]) is True

    def test_maker_reviewer_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        result = cast(
            "dict[str, object]",
            call_command(
                "review",
                "record-evidence",
                str(ticket.pk),
                "--reviewer",
                "merge-loop",
                "--verdict",
                "merge_safe",
                "--head-sha",
                _SHA,
            ),
        )
        assert result["recorded"] is False
        assert ReviewEvidence.objects.for_ticket(ticket).count() == 0

    def test_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command(
                "review",
                "record-evidence",
                "999999",
                "--reviewer",
                "r",
                "--verdict",
                "ok",
                "--head-sha",
                _SHA,
            )

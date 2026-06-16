"""``t3 <overlay> review record`` / ``review status`` + the ``ticket clear`` record seam.

Integration-style: the real guarded factory (``ReviewVerdict.record`` via the
``review record`` management command), the real ``ticket clear`` issuance seam,
real ORM rows. Only the forge head/checks lookups — the unstoppable external
``gh``/``glab`` calls — are stubbed, so the suite never touches the network.

The payoff under test: record a verdict, move the PR head, and ``review
status`` reports *stale* (re-review needed); record at the live head with green
checks and it reports *safe-to-approve*; with nothing recorded it reports *no
verdict*. Issuing a CLEAR records a merge_safe verdict as a by-product so the
two contracts stay coherent.
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import MergeClear, ReviewVerdict
from tests.factories import TicketFactory

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_REVIEWED = "a" * 40
_MOVED = "b" * 40
_URL = "https://github.com/souliane/teatree/pull/1680"
_REVIEW_MOD = "teatree.core.management.commands.review"


def _record(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "reviewed_sha": _REVIEWED,
        "verdict": "merge_safe",
        "reviewer_identity": "cold-reviewer",
        "gh_verify_result": "green",
        "blast_class": "logic",
    }
    kwargs.update(overrides)
    return cast(
        "dict[str, object]",
        call_command("review", "record", "1680", "souliane/teatree", **kwargs),
    )


def _status(*, head: str, checks: str = "green") -> dict[str, object]:
    with (
        patch(f"{_REVIEW_MOD}.fetch_live_head_sha", return_value=head),
        patch(f"{_REVIEW_MOD}.fetch_required_checks_status", return_value=checks),
    ):
        return cast("dict[str, object]", call_command("review", "status", _URL))


class TestRecordCommand(TestCase):
    def test_record_persists_a_verdict_with_findings(self) -> None:
        findings = '[{"severity": "nit", "summary": "rename x", "file": "a.py", "line": 9}]'
        result = _record(findings_json=findings)
        assert result["recorded"]
        assert result["findings_count"] == 1
        stored = ReviewVerdict.objects.get(pk=result["verdict_id"])
        assert stored.structured_findings[0].location() == "a.py:9"

    def test_record_without_reviewed_sha_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            call_command("review", "record", "1680", "souliane/teatree", reviewer_identity="r")

    def test_record_merge_safe_on_red_checks_is_refused(self) -> None:
        result = _record(gh_verify_result="failed")
        assert not result["recorded"]
        assert "green" in cast("str", result["error"]).lower()
        assert ReviewVerdict.objects.count() == 0

    def test_record_invalid_findings_json_is_refused(self) -> None:
        result = _record(findings_json="{not json")
        assert not result["recorded"]
        assert ReviewVerdict.objects.count() == 0

    def test_record_non_array_findings_json_is_refused(self) -> None:
        result = _record(findings_json='{"severity": "nit"}')
        assert not result["recorded"]
        assert "array" in cast("str", result["error"]).lower()
        assert ReviewVerdict.objects.count() == 0

    def test_record_with_unknown_ticket_id_is_refused(self) -> None:
        result = _record(ticket_id=999999)
        assert not result["recorded"]
        assert "not found" in cast("str", result["error"]).lower()
        assert ReviewVerdict.objects.count() == 0


class TestStatusCommand(TestCase):
    def test_no_recorded_verdict_path(self) -> None:
        result = _status(head=_REVIEWED)
        assert result["state"] == "no_verdict"
        assert ReviewVerdict.objects.count() == 0

    def test_safe_to_approve_when_recorded_at_live_head_and_checks_green(self) -> None:
        _record()
        result = _status(head=_REVIEWED, checks="green")
        assert result["state"] == "safe_to_approve"
        assert result["reviewed_sha"] == _REVIEWED
        assert result["current_head_sha"] == _REVIEWED

    def test_stale_when_head_moved_off_reviewed_sha(self) -> None:
        _record()
        result = _status(head=_MOVED)
        assert result["state"] == "stale"
        assert result["reviewed_sha"] == _REVIEWED
        assert result["current_head_sha"] == _MOVED

    def test_not_safe_when_checks_not_green_at_head(self) -> None:
        _record()
        result = _status(head=_REVIEWED, checks="failed")
        assert result["state"] == "not_safe"

    def test_hold_verdict_at_head_reports_not_safe(self) -> None:
        _record(verdict="hold", gh_verify_result="failed")
        result = _status(head=_REVIEWED, checks="green")
        assert result["state"] == "not_safe"
        assert result["verdict"] == ReviewVerdict.Verdict.HOLD

    def test_status_reports_latest_verdict_after_re_review_at_moved_head(self) -> None:
        _record()
        _record(reviewed_sha=_MOVED)
        result = _status(head=_MOVED)
        assert result["state"] == "safe_to_approve"
        assert result["reviewed_sha"] == _MOVED

    def test_unparseable_url_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            call_command("review", "status", "not-a-pr-url")


class TestTicketClearRecordsVerdict(TestCase):
    def test_issuing_a_clear_records_a_merge_safe_verdict_sibling(self) -> None:
        ticket = TicketFactory()
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "1680",
                "souliane/teatree",
                reviewed_sha=_REVIEWED,
                reviewer_identity="cold-reviewer",
                blast_class="docs",
                ticket_id=int(ticket.pk),
            ),
        )
        assert result["issued"]
        verdict = ReviewVerdict.objects.get(pk=result["recorded_verdict_id"])
        assert verdict.is_merge_safe()
        assert verdict.reviewed_sha == _REVIEWED
        assert verdict.blast_class == MergeClear.BlastClass.DOCS
        assert verdict.ticket_id == ticket.pk

    def test_cleared_pr_is_then_safe_to_approve_via_status(self) -> None:
        ticket = TicketFactory()
        call_command(
            "ticket",
            "clear",
            "1680",
            "souliane/teatree",
            reviewed_sha=_REVIEWED,
            reviewer_identity="cold-reviewer",
            ticket_id=int(ticket.pk),
        )
        result = _status(head=_REVIEWED, checks="green")
        assert result["state"] == "safe_to_approve"

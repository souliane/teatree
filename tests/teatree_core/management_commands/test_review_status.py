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

``TestUnreadableForgeIsNotAVerdict`` covers the fourth case: the forge could not
be read at all. Neither half of that — no head, no rollup — is a fact about the
pull request, so neither may be reported as *stale* or as *checks failed*; both
would send a reader off to spend a cold re-review on a tree nobody touched. Those
tests drive the REAL classifier over a stubbed ``gh``, not a patched verdict
constant, so a producer that went back to saying "failed" fails them.
"""

import json
from collections.abc import Callable
from io import StringIO
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.modelkit.diff_scope import ChangedFileSet
from teatree.core.modelkit.forge_readability import HEAD_SHA_UNREADABLE, LiveHeadRead
from teatree.core.models import MergeClear, ReviewVerdict
from teatree.core.review.verdict_findings import FindingsRenderError
from tests.factories import TicketFactory

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_REVIEWED = "a" * 40
_MOVED = "b" * 40
_URL = "https://github.com/souliane/teatree/pull/1680"
_HEAD_READ = "teatree.core.merge.ci_rollup.CodeHostQuery.live_head_read"
_GH_RUNNER = "teatree.backends.forge_merge_rpc.gh_runner"
_REQUIRED_CONTEXT = "test (3.13)"


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


def _refused_record(**overrides: object) -> str:
    """The stderr of a refused ``review record``, asserting the nonzero exit (#932)."""
    err = StringIO()
    with pytest.raises(SystemExit) as exc:
        _record(stderr=err, **overrides)
    assert exc.value.code == 1
    return err.getvalue()


def _status(*, head: str, checks: str = "green") -> dict[str, object]:
    # The head seam is the READ, not the sha: an unreadable forge and a forge
    # reporting no head are different facts, and only the read carries both.
    with (
        patch(_HEAD_READ, return_value=LiveHeadRead.of(head)),
        patch("teatree.core.merge.ci_rollup.CodeHostQuery.required_checks_status", return_value=checks),
    ):
        return cast("dict[str, object]", call_command("review", "status", _URL))


def _gh(*, rollup_rc: int, rollup_body: str) -> Callable[[list[str]], tuple[int, str, str]]:
    """A ``gh`` answering exactly what ``required_checks_status`` asks, over ONE required context.

    Scripting the transport rather than the verdict is the point: these tests must
    fail if the CLASSIFIER stops distinguishing an unreadable rollup from a red one.
    """

    def run(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "statusCheckRollup" in joined:
            return (rollup_rc, rollup_body, "")
        if "baseRefName" in joined:
            return (0, "main", "")
        if "rules/branches" in joined:
            return (1, "", "HTTP 500: server error")
        if "required_status_checks" in joined:
            return (0, json.dumps({"contexts": [_REQUIRED_CONTEXT]}), "")
        return (0, "", "")

    return run


def _red_rollup() -> str:
    return json.dumps(
        [
            {
                "__typename": "CheckRun",
                "name": _REQUIRED_CONTEXT,
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "completedAt": "2026-08-18T10:00:00Z",
            },
        ],
    )


def _status_over_live_checks(*, head: str, rollup_rc: int, rollup_body: str) -> dict[str, object]:
    with (
        patch(_HEAD_READ, return_value=LiveHeadRead.of(head)),
        patch(_GH_RUNNER, return_value=_gh(rollup_rc=rollup_rc, rollup_body=rollup_body)),
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
        # FIX-EXPEDITE: a merge_safe verdict can never carry a FAILED result (even expedited).
        assert "never carry gh_verify_result=failed" in _refused_record(gh_verify_result="failed")
        assert ReviewVerdict.objects.count() == 0

    def test_record_invalid_findings_json_is_refused(self) -> None:
        assert _refused_record(findings_json="{not json")
        assert ReviewVerdict.objects.count() == 0

    def test_record_non_array_findings_json_is_refused(self) -> None:
        assert "array" in _refused_record(findings_json='{"severity": "nit"}').lower()
        assert ReviewVerdict.objects.count() == 0

    def test_record_with_unknown_ticket_id_is_refused(self) -> None:
        assert "not found" in _refused_record(ticket_id=999999).lower()
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


class TestRecordDiffScopeGate(TestCase):
    """``review record`` reads the PR's changed-file set and refuses a branch-only probe (#4251)."""

    _SRC_FINDING = (
        '[{"severity": "high", "summary": "grant is too narrow", '
        '"file": "src/teatree/core/modelkit/phase_tools.py", "line": 41}]'
    )

    def _record_src_finding(self, changed: ChangedFileSet, **overrides: object) -> dict[str, object]:
        with patch(
            "teatree.core.management.commands._review_impl.changed_file_set_for_findings",
            return_value=changed,
        ):
            return _record(verdict="hold", findings_json=self._SRC_FINDING, **overrides)

    def _refused_src_finding(self, changed: ChangedFileSet, **overrides: object) -> str:
        err = StringIO()
        with pytest.raises(SystemExit) as exc:
            self._record_src_finding(changed, stderr=err, **overrides)
        assert exc.value.code == 1
        return err.getvalue()

    def test_a_blocking_finding_outside_the_changed_set_is_refused(self) -> None:
        refusal = self._refused_src_finding(ChangedFileSet.known(["skills/review/SKILL.md"]))

        assert "t3 review merge-tree" in refusal
        assert ReviewVerdict.objects.count() == 0

    def test_the_merge_result_retake_flag_records_the_same_finding(self) -> None:
        result = self._record_src_finding(ChangedFileSet.known(["skills/review/SKILL.md"]), merge_result_retake=True)

        assert result["recorded"]
        assert ReviewVerdict.objects.count() == 1

    def test_a_verdict_with_no_blocking_citation_never_reads_the_diff(self) -> None:
        with patch("teatree.core.review.diff_scope_probe.changed_file_set_for") as fetch:
            result = _record(findings_json='[{"severity": "nit", "summary": "rename x", "file": "a.py"}]')

        fetch.assert_not_called()
        assert result["recorded"]


class TestUnreadableForgeIsNotAVerdict(TestCase):
    """A forge nobody could read says nothing about the PR — and must not pretend to."""

    def test_an_unreadable_head_is_not_reported_as_stale(self) -> None:
        """The measured incident: a merge_safe PR, untouched, reported stale mid-503.

        ``is_stale_at("")`` is ``reviewed_sha != ""`` — always true — so every head
        read the forge failed to answer arrived at the reader as head drift, and each
        one cost a full cold re-review of a tree that had not moved.
        """
        _record()
        result = _status(head=HEAD_SHA_UNREADABLE)
        assert result["state"] == "head_unreadable"
        assert result["verdict"] == ReviewVerdict.Verdict.MERGE_SAFE

    def test_a_forge_that_names_no_head_is_also_not_reported_as_stale(self) -> None:
        # A readable-but-degraded payload carrying no oid is the same non-answer: a
        # real PR always has a head, so "" is a failed read, not a moved branch.
        _record()
        assert _status(head="")["state"] == "head_unreadable"

    def test_an_unreadable_rollup_is_not_reported_as_failing_checks(self) -> None:
        """The rollup query itself 503s: no verdict exists, so none may be reported."""
        _record()
        result = _status_over_live_checks(head=_REVIEWED, rollup_rc=1, rollup_body="")
        assert result["state"] == "checks_unreadable"
        assert result["verdict"] == ReviewVerdict.Verdict.MERGE_SAFE

    def test_a_genuinely_failing_required_check_still_reports_failed(self) -> None:
        """The over-correction guard: a real red must NOT be laundered into "unreadable".

        "Stop saying failed" is otherwise satisfiable by never saying it, which would
        report a genuinely red PR as a forge hiccup worth retrying.
        """
        _record()
        result = _status_over_live_checks(head=_REVIEWED, rollup_rc=0, rollup_body=_red_rollup())
        assert result["state"] == "not_safe"
        assert result["live_checks"] == "failed"

    def test_a_recorded_verdict_survives_an_unreadable_head_unchanged(self) -> None:
        # The whole payoff: the row is untouched, so the retry that follows the hiccup
        # answers safe-to-approve from the SAME verdict rather than from a new review.
        _record()
        _status(head=HEAD_SHA_UNREADABLE)
        assert _status(head=_REVIEWED, checks="green")["state"] == "safe_to_approve"
        assert ReviewVerdict.objects.count() == 1


class TestStatusDegradesOnUnrenderableFindings(TestCase):
    """`review status` answers; `review findings` still refuses (#4575).

    Whether a finding RENDERS is unrelated to whether the head is SAFE TO APPROVE, so an
    unrenderable display row must not turn a read-only gate into a blocked decision. Both
    rows below were reproduced live during a cold review; they are written past the guarded
    factory because that is how legacy rows reached the column.
    """

    def _record_findings(self, findings: object) -> None:
        _record()
        ReviewVerdict.objects.filter(reviewed_sha=_REVIEWED).update(findings=findings)

    def test_status_returns_its_verdict_when_a_findings_row_is_a_non_dict(self) -> None:
        self._record_findings([{"severity": "nit", "summary": "ok"}, "just a string"])
        result = _status(head=_REVIEWED, checks="green")
        assert result["state"] == "safe_to_approve"
        assert result["findings"] == []
        assert result["findings_count"] == 2
        assert "finding 1 is str" in cast("str", result["findings_error"])

    def test_status_returns_its_verdict_when_a_findings_row_has_an_empty_summary(self) -> None:
        self._record_findings([{"severity": "blocker", "summary": "   ", "file": "a.py", "line": 1}])
        result = _status(head=_REVIEWED, checks="green")
        assert result["state"] == "safe_to_approve"
        assert result["findings_count"] == 1
        assert "empty summary" in cast("str", result["findings_error"])

    def test_status_returns_its_verdict_when_findings_is_not_a_list(self) -> None:
        self._record_findings({"severity": "nit", "summary": "x"})
        result = _status(head=_REVIEWED, checks="green")
        assert result["state"] == "safe_to_approve"
        assert result["findings_count"] == 0
        assert result["findings_error"]

    def test_a_hold_over_unrenderable_findings_still_reports_not_safe(self) -> None:
        _record(verdict="hold", gh_verify_result="failed")
        ReviewVerdict.objects.filter(reviewed_sha=_REVIEWED).update(findings=["just a string"])
        result = _status(head=_REVIEWED, checks="green")
        assert result["state"] == "not_safe"
        assert result["verdict"] == ReviewVerdict.Verdict.HOLD

    def test_a_well_formed_findings_row_is_unchanged(self) -> None:
        _record(findings_json='[{"severity": "nit", "summary": "rename x", "file": "a.py", "line": 9}]')
        result = _status(head=_REVIEWED, checks="green")
        assert result["state"] == "safe_to_approve"
        assert result["findings_count"] == 1
        assert cast("list[dict[str, object]]", result["findings"])[0]["summary"] == "rename x"
        assert "findings_error" not in result

    def _assert_findings_refuses(self, findings: object, expected: str) -> None:
        self._record_findings(findings)
        with pytest.raises(FindingsRenderError) as exc:
            call_command("review", "findings", _URL)
        assert expected in str(exc.value)

    def test_review_findings_still_raises_on_a_non_dict_row(self) -> None:
        self._assert_findings_refuses([{"severity": "nit", "summary": "ok"}, "just a string"], "finding 1 is str")

    def test_review_findings_still_raises_on_an_empty_summary_row(self) -> None:
        self._assert_findings_refuses(
            [{"severity": "blocker", "summary": "  ", "file": "a.py", "line": 1}], "empty summary"
        )

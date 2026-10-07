"""A recorded verdict's findings render, or refuse loudly — never a count over nothing (#4476).

The gap this pins: ``review status`` counted the RAW ``findings`` JSON rows while
``structured_findings`` dropped every non-dict one, so ``findings_count: 4`` could
stand in front of content that renders as three lines, or none. The strict read
refuses that payload instead of quietly shrinking it.
"""

import pytest
from django.test import TestCase

from teatree.core.models import ReviewVerdict
from teatree.core.review.verdict_findings import (
    FindingsRenderError,
    findings_payload,
    marker_for,
    readable_findings,
    render_findings_text,
    render_review_parts,
)

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "c" * 40


def _verdict(findings: list[object], *, sha: str = _SHA, verdict: str = "hold") -> ReviewVerdict:
    return ReviewVerdict.objects.create(
        slug="souliane/teatree",
        pr_id=4476,
        reviewed_sha=sha,
        verdict=verdict,
        reviewer_identity="cold-reviewer",
        findings=findings,
        blast_class="logic",
        gh_verify_result="green",
    )


class TestStrictPayload(TestCase):
    def test_well_formed_findings_round_trip(self) -> None:
        verdict = _verdict(
            [
                {"severity": "blocker", "summary": "unbounded loop", "file": "a.py", "line": 9},
                {"severity": "nit", "summary": "rename x", "file": "", "line": 0},
            ]
        )
        payload = findings_payload(verdict)
        assert [row["summary"] for row in payload] == ["unbounded loop", "rename x"]
        assert payload[0]["line"] == 9

    def test_a_non_object_row_is_refused_not_dropped(self) -> None:
        verdict = _verdict([{"severity": "nit", "summary": "ok"}, "just a string"])
        with pytest.raises(FindingsRenderError) as exc:
            findings_payload(verdict)
        assert "finding 1 is str" in str(exc.value)

    def test_a_row_with_no_summary_is_refused(self) -> None:
        verdict = _verdict([{"severity": "blocker", "summary": "   ", "file": "a.py", "line": 1}])
        with pytest.raises(FindingsRenderError) as exc:
            findings_payload(verdict)
        assert "empty summary" in str(exc.value)

    def test_a_non_list_payload_is_refused(self) -> None:
        verdict = _verdict([])
        ReviewVerdict.objects.filter(pk=verdict.pk).update(findings={"severity": "nit"})
        verdict.refresh_from_db()
        with pytest.raises(FindingsRenderError):
            findings_payload(verdict)

    def test_the_strict_read_refuses_where_structured_findings_silently_drops(self) -> None:
        verdict = _verdict([{"severity": "nit", "summary": "ok"}, 7])
        assert len(verdict.structured_findings) == 1
        assert len(verdict.findings) == 2
        with pytest.raises(FindingsRenderError):
            findings_payload(verdict)


class TestRenderers(TestCase):
    def test_text_view_carries_every_finding_and_its_location(self) -> None:
        verdict = _verdict(
            [
                {"severity": "blocker", "summary": "unbounded loop", "file": "a.py", "line": 9},
                {"severity": "major", "summary": "no rollback", "file": "", "line": 0},
            ]
        )
        rendered = render_findings_text(verdict)
        assert "a.py:9" in rendered
        assert "unbounded loop" in rendered
        assert "(MR-level)" in rendered
        assert "no rollback" in rendered
        assert "cold-reviewer" in rendered

    def test_text_view_says_so_when_no_findings_are_recorded(self) -> None:
        assert "no findings recorded" in render_findings_text(_verdict([]))

    def test_an_anchored_finding_is_its_own_inline_comment_and_the_summary_carries_the_marker(self) -> None:
        verdict = _verdict([{"severity": "blocker", "summary": "unbounded loop", "file": "a.py", "line": 9}])
        parts = render_review_parts(verdict)
        assert [(c.path, c.line, c.body) for c in parts.comments] == [("a.py", 9, "**[blocker]** unbounded loop")]
        assert marker_for(verdict) in parts.summary
        assert "unbounded loop" not in parts.summary

    def test_an_unanchored_finding_stays_in_the_summary(self) -> None:
        parts = render_review_parts(
            _verdict([{"severity": "minor", "summary": "log the retry", "file": "a.py", "line": 0}])
        )
        assert not parts.comments
        assert "log the retry" in parts.summary

    def test_a_merge_safe_review_carries_no_hold_text_reviewer_or_operator_command(self) -> None:
        verdict = _verdict(
            [
                {"severity": "nit", "summary": "rename x", "file": "a.py", "line": 9},
                {"severity": "minor", "summary": "log the retry", "file": "", "line": 0},
            ],
            verdict="merge_safe",
        )
        parts = render_review_parts(verdict)
        text = "\n".join([parts.summary, *(c.body for c in parts.comments)])
        assert "rename x" in text
        assert "log the retry" in text
        assert "merge_safe" in text
        assert "hold" not in text.lower()
        assert "cold-reviewer" not in text
        assert "t3 <overlay>" not in text

    def test_a_hold_summary_names_the_hold(self) -> None:
        verdict = _verdict([{"severity": "blocker", "summary": "unbounded loop", "file": "a.py", "line": 9}])
        assert "hold" in render_review_parts(verdict).summary

    def test_review_parts_refuse_an_empty_verdict(self) -> None:
        with pytest.raises(FindingsRenderError):
            render_review_parts(_verdict([]))

    def test_the_marker_is_verdict_specific(self) -> None:
        first = _verdict([{"severity": "nit", "summary": "a"}])
        second = _verdict([{"severity": "nit", "summary": "b"}], sha="d" * 40)
        assert marker_for(second) not in render_review_parts(first).summary


class TestLenientRead(TestCase):
    """The read-only sibling: a display defect degrades, it never blocks a decision (#4575)."""

    def test_a_well_formed_payload_matches_the_strict_read(self) -> None:
        verdict = _verdict(
            [
                {"severity": "blocker", "summary": "unbounded loop", "file": "a.py", "line": 9},
                {"severity": "nit", "summary": "rename x"},
            ]
        )
        readable = readable_findings(verdict)
        assert readable.payload == findings_payload(verdict)
        assert readable.error == ""
        assert readable.recorded_count == 2

    def test_a_non_object_row_degrades_and_names_the_reason(self) -> None:
        readable = readable_findings(_verdict([{"severity": "nit", "summary": "ok"}, "just a string"]))
        assert readable.payload == []
        assert "finding 1 is str" in readable.error
        assert readable.recorded_count == 2

    def test_a_row_with_no_summary_degrades_and_names_the_reason(self) -> None:
        readable = readable_findings(_verdict([{"severity": "blocker", "summary": "   ", "file": "a.py", "line": 1}]))
        assert readable.payload == []
        assert "empty summary" in readable.error

    def test_a_non_list_payload_degrades_without_counting_its_keys(self) -> None:
        verdict = _verdict([])
        ReviewVerdict.objects.filter(pk=verdict.pk).update(findings={"severity": "nit", "summary": "x"})
        verdict.refresh_from_db()
        readable = readable_findings(verdict)
        assert readable.payload == []
        assert readable.error != ""
        assert readable.recorded_count == 0

    def test_an_empty_payload_is_not_an_error(self) -> None:
        readable = readable_findings(_verdict([]))
        assert readable.payload == []
        assert readable.error == ""
        assert readable.recorded_count == 0

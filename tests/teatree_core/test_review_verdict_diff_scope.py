"""``ReviewVerdict.record`` refuses a blocking finding about untouched code (#4251).

The guarded factory is where every recording path funnels, so the diff-scope
gate sits beside the existing full-SHA / verdict / verify-result refusals. The
anti-vacuity controls matter as much as the refusal: an in-scope finding, a
non-blocking one, and an UNREADABLE changed-file set must all still record.
"""

import pytest
from django.test import TestCase

from teatree.core.modelkit.diff_scope import ChangedFileSet
from teatree.core.models import Finding, ReviewVerdict, ReviewVerdictError

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "c" * 40
_DOCS_ONLY = ChangedFileSet.known(["skills/review/SKILL.md"])
_SRC_FINDING = Finding(
    severity="high",
    summary="tools_for_phase grants no write/edit, so every dispatch parks",
    file="src/teatree/core/modelkit/phase_tools.py",
    line=41,
)


def _record(
    *,
    findings: list[Finding] | None = None,
    changed_files: ChangedFileSet | None = None,
    merge_result_retake: bool = False,
) -> ReviewVerdict:
    return ReviewVerdict.record(
        pr_id=4230,
        slug="souliane/teatree",
        reviewed_sha=_SHA,
        verdict="hold",
        reviewer_identity="cold-reviewer",
        findings=findings,
        changed_files=changed_files,
        merge_result_retake=merge_result_retake,
    )


class TestDiffScopeGate(TestCase):
    def test_a_blocking_finding_outside_the_changed_set_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError) as excinfo:
            _record(findings=[_SRC_FINDING], changed_files=_DOCS_ONLY)

        assert "src/teatree/core/modelkit/phase_tools.py" in str(excinfo.value)
        assert "t3 review merge-tree" in str(excinfo.value)
        assert ReviewVerdict.objects.count() == 0

    def test_an_attested_merge_result_retake_records_the_same_finding(self) -> None:
        verdict = _record(findings=[_SRC_FINDING], changed_files=_DOCS_ONLY, merge_result_retake=True)

        assert verdict.structured_findings[0].file == _SRC_FINDING.file

    def test_a_blocking_finding_on_a_changed_file_records(self) -> None:
        in_scope = Finding(severity="blocker", summary="stale guidance", file="skills/review/SKILL.md", line=3)

        assert _record(findings=[in_scope], changed_files=_DOCS_ONLY).structured_findings[0].file == in_scope.file

    def test_a_non_blocking_finding_outside_the_changed_set_records(self) -> None:
        nit = Finding(severity="nit", summary="rename x", file="src/teatree/paths.py", line=9)

        assert _record(findings=[nit], changed_files=_DOCS_ONLY).structured_findings[0].file == nit.file

    def test_an_unreadable_changed_set_never_refuses(self) -> None:
        verdict = _record(findings=[_SRC_FINDING], changed_files=ChangedFileSet.unavailable())

        assert verdict.structured_findings[0].file == _SRC_FINDING.file

    def test_a_caller_that_supplies_no_changed_set_is_unchanged(self) -> None:
        assert _record(findings=[_SRC_FINDING]).structured_findings[0].file == _SRC_FINDING.file

    def test_a_carried_forward_verdict_keeps_its_already_gated_findings(self) -> None:
        recorded = _record(findings=[_SRC_FINDING], changed_files=_DOCS_ONLY, merge_result_retake=True)

        carried = recorded.carry_forward(reviewed_sha="d" * 40)

        assert carried.structured_findings[0].file == _SRC_FINDING.file

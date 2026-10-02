"""The retro review-findings filer routes forge writes through the #117 seam (U14).

``file_class_c_issue`` filed enforcement issues via a raw ``host.create_issue``
with no leak scrub or #117 audit — laxer than the MCP surface. It now routes the
title + body through :func:`teatree.core.send_proxy.route_forge_write`, so a
SendAudit row is written and a leaking body is WITHHELD before the backend call.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import SendAudit
from teatree.core.review.review_findings import FilingContext, ReviewFinding, file_class_c_issue

_CONTEXT = FilingContext(repo="o/r", pr_url="https://github.com/o/r/pull/1")


class _FakeHost:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    def search_open_issues(self, *, repo: str, query: str) -> list[dict[str, object]]:
        return []

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, object]:
        self.created.append({"repo": repo, "title": title, "body": body, "labels": labels})
        return {"html_url": f"https://github.com/{repo}/issues/7", "number": 7}


def _finding(body: str = "Use a context manager here.") -> ReviewFinding:
    return ReviewFinding(body=body, path="src/a.py", line=12, author="reviewer")


class ReviewFindingsRouteThroughSeam(TestCase):
    def test_filing_a_class_c_issue_writes_a_send_audit_row(self) -> None:
        host = _FakeHost()
        with patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False):
            result = file_class_c_issue(host, finding=_finding(), enforcement="add a gate", context=_CONTEXT)
        assert result.withheld is False
        assert host.created
        assert SendAudit.objects.filter(destination="o/r", action="retro_review_finding").exists()

    def test_a_leaking_body_is_withheld_before_the_backend(self) -> None:
        host = _FakeHost()
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["SECRETCORP"], [])),
        ):
            result = file_class_c_issue(
                host, finding=_finding(body="Leak SECRETCORP here."), enforcement="add a gate", context=_CONTEXT
            )
        assert result.withheld is True
        assert "privacy gate refused" in (result.withheld_reason or "")
        assert host.created == []

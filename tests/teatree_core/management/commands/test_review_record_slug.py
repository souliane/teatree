"""``t3 <overlay> review record`` refuses a branch-shaped slug.

The verdict / merge lookup (``ReviewVerdict.objects.latest_for_pr``, the
pr_sweep trigger) keys by the repo slug ``owner/repo`` parsed from the PR
URL, so a verdict recorded under a branch name can never be found by any
consumer — the command must refuse it at the CLI boundary.
"""

from io import StringIO
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ReviewVerdict

_PR_ID = 41
_HEAD = "497d468df76022b280caffceb400739d5ced9baa"


def _record(slug: str, *, stderr: StringIO) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        call_command(
            "review",
            "record",
            str(_PR_ID),
            slug,
            reviewed_sha=_HEAD,
            verdict="hold",
            reviewer_identity="cold-reviewer-agent",
            gh_verify_result="green",
            blast_class="logic",
            stderr=stderr,
        ),
    )


class TestRecordSlugShape(TestCase):
    def test_branch_shaped_slug_is_refused_with_actionable_message(self) -> None:
        stderr = StringIO()

        with pytest.raises(SystemExit):
            _record("my-feature-branch", stderr=stderr)

        message = stderr.getvalue()
        assert "slug must be owner/repo (got 'my-feature-branch')" in message
        assert "this looks like a branch name" in message
        assert "merge lookup keys by repo slug" in message
        assert ReviewVerdict.objects.count() == 0

    def test_owner_repo_slug_is_accepted(self) -> None:
        stderr = StringIO()

        result = _record("acme/widgets", stderr=stderr)

        assert result["recorded"] is True
        assert ReviewVerdict.objects.get().slug == "acme/widgets"

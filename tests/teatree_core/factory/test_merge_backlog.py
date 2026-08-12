"""``merge_backlog`` — the supersede predicate every merge-scoped reader shares (#4250).

The slug-keyed signals miss a real case: two CLEARs for the SAME PR and the SAME
reviewed tree, one of which stored a head branch (``review-fixes/docs``) where the
other stored the repo. The merged sibling carries the ``MergeAudit``, the branch-slugged
orphan does not, and ``(slug, pr_id)`` never matches across the two — so the alarm
reported a diff that had demonstrably merged. The ``(pr_id, reviewed_sha)`` key closes it
without touching slug resolution (that blind spot is #4249's).
"""

from datetime import timedelta

import django.test
from django.utils import timezone

from teatree.core.factory.merge_backlog import (
    clear_is_superseded,
    superseding_context,
    unconsumed_actionable_clear_rows,
    unconsumed_actionable_clears,
)
from tests.factories import MergeAuditFactory, MergeClearFactory

_SHA = "a" * 40


class SupersedeKeyTests(django.test.TestCase):
    def setUp(self) -> None:
        self.now = timezone.now()

    def test_a_sibling_audit_on_the_same_sha_supersedes_a_branch_slugged_orphan(self) -> None:
        # Mirrors live CLEARs 578/579: same pr_id 4230, same reviewed_sha, one slugged
        # `review-fixes/docs` (a head branch) and one `souliane/teatree`. The merged one
        # carries the audit; the orphan must not read as a standing authorisation.
        orphan = MergeClearFactory(
            ticket=None,
            pr_id=4230,
            slug="review-fixes/docs",
            reviewed_sha=_SHA,
            issued_at=self.now - timedelta(hours=146),
        )
        merged = MergeClearFactory(
            ticket=None,
            pr_id=4230,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            issued_at=self.now - timedelta(hours=146),
            consumed_at=self.now,
        )
        MergeAuditFactory(clear=merged, merged_at=self.now)

        assert clear_is_superseded(orphan, superseding_context("")) is True
        assert unconsumed_actionable_clears("", self.now) == []

    def test_a_different_sha_on_the_same_pr_is_not_superseded(self) -> None:
        # The sha is what makes the key safe: a genuinely newer diff on the same PR
        # has its own reviewed tree, so a sibling audit must not silence it.
        standing = MergeClearFactory(
            ticket=None,
            pr_id=4230,
            slug="review-fixes/docs",
            reviewed_sha="b" * 40,
            issued_at=self.now - timedelta(hours=146),
        )
        merged = MergeClearFactory(
            ticket=None, pr_id=4230, slug="souliane/teatree", reviewed_sha=_SHA, consumed_at=self.now
        )
        MergeAuditFactory(clear=merged, merged_at=self.now)

        assert clear_is_superseded(standing, superseding_context("")) is False

    def test_the_same_sha_on_a_different_pr_is_not_superseded(self) -> None:
        standing = MergeClearFactory(ticket=None, pr_id=4231, slug="souliane/other", reviewed_sha=_SHA)
        merged = MergeClearFactory(
            ticket=None, pr_id=4230, slug="souliane/teatree", reviewed_sha=_SHA, consumed_at=self.now
        )
        MergeAuditFactory(clear=merged, merged_at=self.now)

        assert clear_is_superseded(standing, superseding_context("")) is False

    def test_the_sha_key_is_case_insensitive(self) -> None:
        orphan = MergeClearFactory(ticket=None, pr_id=4230, slug="review-fixes/docs", reviewed_sha=_SHA.upper())
        merged = MergeClearFactory(
            ticket=None, pr_id=4230, slug="souliane/teatree", reviewed_sha=_SHA, consumed_at=self.now
        )
        MergeAuditFactory(clear=merged, merged_at=self.now)

        assert clear_is_superseded(orphan, superseding_context("")) is True

    def test_the_slug_keyed_signals_still_apply(self) -> None:
        # Behaviour preservation: the #15 signals are widened, never replaced.
        older = MergeClearFactory(
            ticket=None, pr_id=99, slug="souliane/teatree", issued_at=self.now - timedelta(hours=5)
        )
        MergeClearFactory(ticket=None, pr_id=99, slug="souliane/teatree", issued_at=self.now)

        assert clear_is_superseded(older, superseding_context("")) is True


class BacklogRowsTests(django.test.TestCase):
    def setUp(self) -> None:
        self.now = timezone.now()

    def test_the_row_form_and_the_report_form_describe_one_population(self) -> None:
        # Two surfaces answering the same question from two queries is the defect this
        # module exists to prevent — the DTO form must be a projection of the row form.
        for offset in range(3):
            MergeClearFactory(
                ticket=None,
                pr_id=8000 + offset,
                slug="souliane/teatree",
                issued_at=self.now - timedelta(hours=10 + offset),
            )

        rows = unconsumed_actionable_clear_rows("")
        report = unconsumed_actionable_clears("", self.now)

        assert [row.pr_id for row in rows] == [row.pr_id for row in report]
        assert [row.issued_at for row in rows] == sorted(row.issued_at for row in rows)

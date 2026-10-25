from unittest.mock import patch

from django.test import TestCase

from teatree.core.merge.uncleared_merge import merge_without_clear
from teatree.core.models import MergeAudit, MergeClear
from teatree.utils.pr_ref import PrRef
from tests.teatree_core.conftest import record_owned_pr_for_test, seed_merge_safe_verdict

SLUG = "acme/widget"
HEAD = "a" * 40
MERGED = "b" * 40
REF = PrRef(slug=SLUG, pr_id=17)


class TestUnclearedMerge(TestCase):
    def test_audit_records_the_post_merge_forge_checks_status(self) -> None:
        record_owned_pr_for_test(slug=SLUG, pr_id=17, head_sha=HEAD)
        seed_merge_safe_verdict(slug=SLUG, pr_id=17, sha=HEAD)
        with (
            patch("teatree.core.merge.uncleared_merge.execute_bound_merge", return_value=MERGED),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.for_ref") as query,
        ):
            query.return_value.required_checks_status.return_value = "pending"
            merge_without_clear(ref=REF, expected_head_oid=HEAD)
        assert MergeAudit.objects.get(merged_sha=MERGED).required_checks_status == "pending"

    def test_retry_does_not_duplicate_the_audit(self) -> None:
        record_owned_pr_for_test(slug=SLUG, pr_id=17, head_sha=HEAD)
        seed_merge_safe_verdict(slug=SLUG, pr_id=17, sha=HEAD)
        with (
            patch("teatree.core.merge.uncleared_merge.execute_bound_merge", return_value=MERGED),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.for_ref") as query,
        ):
            query.return_value.required_checks_status.return_value = "green"
            assert merge_without_clear(ref=REF, expected_head_oid=HEAD) == MERGED
            assert merge_without_clear(ref=REF, expected_head_oid=HEAD) == MERGED
        assert MergeAudit.objects.filter(merged_sha=MERGED).count() == 1

    def test_missing_verdict_leaves_the_merge_for_the_alert(self) -> None:
        with patch("teatree.core.merge.uncleared_merge.execute_bound_merge", return_value=MERGED):
            assert merge_without_clear(ref=REF, expected_head_oid=HEAD) == MERGED
        assert not MergeAudit.objects.filter(merged_sha=MERGED).exists()

    def test_post_hook_failure_does_not_report_the_forge_merge_as_refused(self) -> None:
        seed_merge_safe_verdict(slug=SLUG, pr_id=17, sha=HEAD)
        failure = RuntimeError("db unavailable")
        with (
            patch("teatree.core.merge.uncleared_merge.execute_bound_merge", return_value=MERGED),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.for_ref") as query,
            patch("teatree.core.merge.uncleared_merge.record_merge_and_advance", side_effect=failure),
        ):
            query.return_value.required_checks_status.return_value = "green"
            assert merge_without_clear(ref=REF, expected_head_oid=HEAD) == MERGED
        assert not MergeAudit.objects.filter(merged_sha=MERGED).exists()
        assert not MergeClear.objects.filter(slug=SLUG, pr_id=17).exists()

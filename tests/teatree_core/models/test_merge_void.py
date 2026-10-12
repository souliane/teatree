import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models import MergeVoid, Ticket
from teatree.core.models.merge_void import MergeVoidFields

_SHA = "c" * 40


def void_fields(
    *, slug: str = "example-org/example-repo", pr_id: int = 41, host_kind: str = "github"
) -> MergeVoidFields:
    return MergeVoidFields(
        host_kind=host_kind,
        slug=slug,
        pr_id=pr_id,
        pr_url=f"https://github.com/{slug}/pull/{pr_id}",
        merged_sha=_SHA,
        content_head="d" * 40,
        merge_base="e" * 40,
        default_branch="main",
        default_tip="f" * 40,
        changed_paths=["src/a.py", "src/b.py"],
        evidence={"tickets": [], "kept": [], "pull_requests": [], "open_clears": []},
        reason="history rewrite dropped the change",
        actor="alice",
    )


class TestMergeVoidKeying(TestCase):
    def test_one_void_per_pr_whatever_the_slug_case(self) -> None:
        MergeVoid.objects.record(void_fields(), tickets=[])

        with pytest.raises(IntegrityError), transaction.atomic():
            MergeVoid.objects.record(void_fields(slug="Example-Org/Example-Repo"), tickets=[])

    def test_the_same_number_on_another_host_is_another_pr(self) -> None:
        MergeVoid.objects.record(void_fields(), tickets=[])

        MergeVoid.objects.record(void_fields(host_kind="gitlab"), tickets=[])

        assert MergeVoid.objects.count() == 2

    def test_blank_reason_or_actor_is_refused_by_the_database(self) -> None:
        no_reason, no_actor = void_fields(), void_fields()
        no_reason["reason"] = ""
        no_actor["actor"] = ""
        for fields in (no_reason, no_actor):
            with self.subTest(fields=fields), pytest.raises(IntegrityError), transaction.atomic():
                MergeVoid.objects.record(fields, tickets=[])

    def test_record_links_the_restored_tickets(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")

        void = MergeVoid.objects.record(void_fields(), tickets=[ticket])

        assert list(ticket.merge_voids.all()) == [void]


class TestVoidedRefs(TestCase):
    def test_covers_the_voided_pr_case_insensitively_and_nothing_else(self) -> None:
        MergeVoid.objects.record(void_fields(), tickets=[])

        refs = MergeVoid.objects.voided_refs()

        assert refs.covers(slug="EXAMPLE-ORG/example-repo", pr_id=41, host_kind="github")
        assert not refs.covers(slug="example-org/example-repo", pr_id=42, host_kind="github")
        assert not refs.covers(slug="example-org/other-repo", pr_id=41, host_kind="github")
        assert not refs.covers(slug="example-org/example-repo", pr_id=41, host_kind="gitlab")

    def test_evidence_that_names_no_host_is_covered(self) -> None:
        MergeVoid.objects.record(void_fields(), tickets=[])

        assert MergeVoid.objects.voided_refs().covers(slug="example-org/example-repo", pr_id=41)

    def test_for_pr_matches_the_slug_case_insensitively(self) -> None:
        void = MergeVoid.objects.record(void_fields(), tickets=[])

        assert list(MergeVoid.objects.for_pr(slug="Example-Org/Example-Repo", pr_id=41)) == [void]
        assert not MergeVoid.objects.for_pr(slug="example-org/example-repo", pr_id=41, host_kind="gitlab").exists()

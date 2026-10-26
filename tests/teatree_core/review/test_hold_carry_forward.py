import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import ReviewVerdict, Ticket
from teatree.core.review.hold_carry_forward import HoldCarryUnreadableError
from teatree.loop.scanners.pr_sweep_head_carry import look_up_clear
from teatree.loop.scanners.pr_sweep_types import PrSummary
from tests._git_repo import make_git_repo, run_git


class TestHoldCarryForward(TestCase):
    def test_newer_pass_on_an_orphan_head_does_not_hide_a_citable_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = make_git_repo(Path(directory) / "repo")
            run_git(repo, "checkout", "-q", "-b", "feature")
            (repo / "blocked.py").write_text("broken\n")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-q", "-m", "held")
            held = run_git(repo, "rev-parse", "HEAD")
            ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/1")
            common = {
                "ticket": ticket,
                "slug": "example/repo",
                "pr_id": 2,
                "reviewer_identity": "cold-reviewer-2",
                "reviewer_identity_normalized": "cold-reviewer-2",
                "blast_class": "logic",
                "gh_verify_result": "green",
            }
            ReviewVerdict.objects.create(
                **common,
                reviewed_sha=held,
                verdict="hold",
                findings=[
                    {"severity": "major", "kind": "blocking", "summary": "Broken", "file": "blocked.py", "line": 1}
                ],
            )
            ReviewVerdict.objects.create(**common, reviewed_sha="f" * 40, verdict="merge_safe", findings=[])
            (repo / "other.py").write_text("new\n")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-q", "-m", "unrelated")
            head = run_git(repo, "rev-parse", "HEAD")
            pr = PrSummary(slug="example/repo", number=2, head_sha=head, is_draft=False, has_changes_requested=False)

            with patch("teatree.core.review.hold_carry_forward.matching_clone", return_value=repo):
                look_up_clear(pr, reader=None)

            assert ReviewVerdict.objects.filter(
                slug="example/repo", pr_id=2, reviewed_sha=head, verdict="hold"
            ).exists()

    def test_uncited_hold_needs_no_clone_probe(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/1")
        ReviewVerdict.objects.create(
            ticket=ticket,
            slug="example/repo",
            pr_id=2,
            reviewed_sha="a" * 40,
            verdict="hold",
            reviewer_identity="cold-reviewer-2",
            reviewer_identity_normalized="cold-reviewer-2",
            blast_class="logic",
            gh_verify_result="green",
            findings=[],
        )
        pr = PrSummary(slug="example/repo", number=2, head_sha="b" * 40, is_draft=False, has_changes_requested=False)

        with (
            patch("teatree.core.review.hold_carry_forward.matching_clone", side_effect=AssertionError),
        ):
            look_up_clear(pr, reader=None)

    def test_unrelated_push_carries_hold_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = make_git_repo(Path(directory) / "repo")
            run_git(repo, "checkout", "-q", "-b", "feature")
            (repo / "blocked.py").write_text("broken\n")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-q", "-m", "reviewed")
            held = run_git(repo, "rev-parse", "HEAD")
            ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/1")
            ReviewVerdict.objects.create(
                ticket=ticket,
                slug="example/repo",
                pr_id=2,
                reviewed_sha=held,
                verdict="hold",
                reviewer_identity="cold-reviewer-2",
                reviewer_identity_normalized="cold-reviewer-2",
                blast_class="logic",
                gh_verify_result="green",
                findings=[
                    {"severity": "major", "kind": "blocking", "summary": "Broken", "file": "blocked.py", "line": 1}
                ],
            )
            (repo / "other.py").write_text("fixed\n")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-q", "-m", "unrelated")
            head = run_git(repo, "rev-parse", "HEAD")

            pr = PrSummary(slug="example/repo", number=2, head_sha=head, is_draft=False, has_changes_requested=False)
            with (
                patch("teatree.core.review.hold_carry_forward.matching_clone", return_value=None),
                pytest.raises(HoldCarryUnreadableError),
            ):
                look_up_clear(pr, reader=None)
            with patch("teatree.core.review.hold_carry_forward.matching_clone", return_value=repo):
                look_up_clear(pr, reader=None)
            assert ReviewVerdict.objects.filter(
                slug="example/repo", pr_id=2, reviewed_sha=head, verdict="hold"
            ).exists()
            ticket.refresh_from_db()
            assert ticket.extra["hold_carry_forward"][f"example/repo#2:{head}:cold-reviewer-2"] == held
            with patch("teatree.core.review.hold_carry_forward.matching_clone", return_value=repo):
                look_up_clear(pr, reader=None)
            assert ReviewVerdict.objects.filter(slug="example/repo", pr_id=2, reviewed_sha=head).count() == 1

    def test_changed_finding_blob_does_not_carry_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = make_git_repo(Path(directory) / "repo")
            run_git(repo, "checkout", "-q", "-b", "feature")
            (repo / "blocked.py").write_text("broken\n")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-q", "-m", "reviewed")
            held = run_git(repo, "rev-parse", "HEAD")
            ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/1")
            ReviewVerdict.objects.create(
                ticket=ticket,
                slug="example/repo",
                pr_id=2,
                reviewed_sha=held,
                verdict="hold",
                reviewer_identity="cold-reviewer-2",
                reviewer_identity_normalized="cold-reviewer-2",
                blast_class="logic",
                gh_verify_result="green",
                findings=[
                    {"severity": "major", "kind": "blocking", "summary": "Broken", "file": "blocked.py", "line": 1}
                ],
            )
            (repo / "blocked.py").write_text("fixed\n")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-q", "-m", "repair")
            head = run_git(repo, "rev-parse", "HEAD")

            pr = PrSummary(slug="example/repo", number=2, head_sha=head, is_draft=False, has_changes_requested=False)
            with patch("teatree.core.review.hold_carry_forward.matching_clone", return_value=repo):
                look_up_clear(pr, reader=None)
            assert not ReviewVerdict.objects.filter(slug="example/repo", pr_id=2, reviewed_sha=head).exists()

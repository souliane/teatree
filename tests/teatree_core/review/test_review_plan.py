import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import PullRequest, ReviewVerdict, RoundMetric, Rubric, RubricCriterion, Session, Task, Ticket
from teatree.core.review.review_plan import plan_for_task
from tests._git_repo import make_git_repo, run_git


class TestReviewPlan(TestCase):
    def test_new_head_partitions_carried_and_regrade_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = make_git_repo(Path(directory) / "repo")
            run_git(repo, "checkout", "-q", "-b", "feature")
            for name in ("touched.py", "untouched.py", "third.py"):
                (repo / name).write_text("before\n")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-q", "-m", "first review")
            prior = run_git(repo, "rev-parse", "HEAD")
            (repo / "touched.py").write_text("fixed\n")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-q", "-m", "fix")
            head = run_git(repo, "rev-parse", "HEAD")

            url = "https://github.com/example/repo/pull/8"
            owner = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/7")
            PullRequest.objects.create(ticket=owner, url=url, repo="example/repo", iid="8")
            rubric = Rubric.objects.create(ticket=owner)
            criteria = [
                RubricCriterion.objects.create(rubric=rubric, ordinal=index, text=f"criterion {index}")
                for index in range(3)
            ]
            for criterion, rationale in zip(
                criteria,
                ("tests/unchanged.py:1", "touched.py:2", "uncited rationale"),
                strict=True,
            ):
                criterion.record_grade(
                    status="pass", grader_identity="cold-reviewer-one", reviewed_sha=prior, rationale=rationale
                )
            ReviewVerdict.objects.create(
                ticket=owner,
                slug="example/repo",
                pr_id=8,
                reviewed_sha=prior,
                verdict="hold",
                reviewer_identity="cold-reviewer-one",
                reviewer_identity_normalized="cold-reviewer-one",
                blast_class="logic",
                gh_verify_result="green",
                findings=[
                    {
                        "severity": "major",
                        "kind": "blocking",
                        "summary": "fix the branch",
                        "file": "touched.py",
                        "line": 1,
                    }
                ],
            )
            reviewer = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER, extra={"reviewed_sha": head})
            task = Task.objects.create(
                ticket=reviewer, session=Session.objects.create(ticket=reviewer), phase="reviewing"
            )

            with (
                patch("teatree.core.review.review_plan.matching_clone", return_value=repo),
                patch("teatree.core.review.review_plan.resolve_target_branch", return_value="main"),
            ):
                plan = plan_for_task(task)
                repeated = plan_for_task(task)

            assert plan is not None
            assert plan.delta.case == "delta"
            assert plan.carried == frozenset({0})
            assert "CARRIED: #0" in "\n".join(plan.brief_lines())
            assert "RE-GRADE: #1, #2" in "\n".join(plan.brief_lines())
            assert "fix the branch" in "\n".join(plan.brief_lines())
            assert repeated == plan
            assert RoundMetric.objects.filter(ticket=owner, kind="review", subject=head).count() == 1

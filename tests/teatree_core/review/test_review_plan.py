import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.modelkit.forge_readability import LiveHeadRead
from teatree.core.models import (
    HeadCarryOver,
    PullRequest,
    ReviewVerdict,
    RoundMetric,
    Rubric,
    RubricCriterion,
    Session,
    Task,
    TaskAttempt,
    Ticket,
)
from teatree.core.review.review_delta import ReviewDelta
from teatree.core.review.review_plan import plan_for_task
from tests._git_repo import make_git_repo, run_git


def _assert_fallbacks(task: Task, repo: Path, prior: str, head: str) -> None:
    task.ticket.merge_extra(set_keys={"reviewed_sha": "f" * 40})
    with (
        patch("teatree.core.review.review_plan.matching_clone", return_value=repo),
        patch("teatree.core.review.review_plan.resolve_target_branch", return_value="main"),
        patch(
            "teatree.core.review.review_plan.resolve_delta",
            return_value=ReviewDelta("full", "base changed", prior_head=prior),
        ),
    ):
        fallback = plan_for_task(task)
    assert fallback is not None
    assert fallback.head == "f" * 40
    assert fallback.delta.case == "full"
    assert RoundMetric.objects.get(kind="review", subject="f" * 40).size == 0
    assert "Prior reviewed head: " + prior in "\n".join(fallback.brief_lines())
    assert "fix the branch" in "\n".join(fallback.brief_lines())

    task.ticket.merge_extra(set_keys={"reviewed_sha": "e" * 40})
    with (
        patch("teatree.core.review.review_plan.dispatch_worktree_path", return_value=None),
        patch("teatree.core.review.review_plan.matching_clone", return_value=None),
    ):
        no_clone = plan_for_task(task)
    assert no_clone is not None
    assert no_clone.head == "e" * 40
    assert no_clone.delta.reason == "git clone unavailable"
    assert "Prior reviewed head: " + prior in "\n".join(no_clone.brief_lines())

    owner = PullRequest.objects.get(url=task.ticket.issue_url).ticket
    author_task = Task.objects.create(ticket=owner, session=Session.objects.create(ticket=owner), phase="reviewing")
    with patch("teatree.core.review.live_head.live_head_at", return_value=LiveHeadRead.of(head)):
        rebound = plan_for_task(author_task, create=False)
    assert rebound is not None
    assert rebound.head == head

    with (
        patch("teatree.core.review.live_head.live_head_at", return_value=LiveHeadRead(sha="", unreadable=True)),
        patch("teatree.core.review.review_plan.matching_clone", return_value=repo),
    ):
        unreadable = plan_for_task(author_task)
    assert unreadable is None


class TestReviewPlan(TestCase):
    def test_copied_verdict_is_never_the_delta_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = make_git_repo(Path(directory) / "repo")
            run_git(repo, "checkout", "-q", "-b", "feature")
            (repo / "feature.py").write_text("first\n")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-q", "-m", "reviewed")
            reviewed = run_git(repo, "rev-parse", "HEAD")
            run_git(repo, "commit", "--allow-empty", "-m", "head carry")
            copied = run_git(repo, "rev-parse", "HEAD")
            (repo / "feature.py").write_text("fixed\n")
            run_git(repo, "commit", "-am", "fix")
            head = run_git(repo, "rev-parse", "HEAD")
            owner = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/9")
            url = "https://github.com/example/repo/pull/8"
            PullRequest.objects.create(ticket=owner, url=url, repo="example/repo", iid="8")
            for sha in (reviewed, copied):
                ReviewVerdict.objects.create(
                    ticket=owner,
                    slug="example/repo",
                    pr_id=8,
                    reviewed_sha=sha,
                    verdict="hold",
                    reviewer_identity="cold-reviewer-one",
                    reviewer_identity_normalized="cold-reviewer-one",
                    blast_class="logic",
                    gh_verify_result="green",
                    findings=[],
                )
            HeadCarryOver.objects.create(
                slug="example/repo",
                pr_id=8,
                from_head=reviewed,
                to_head=copied,
                outcome=HeadCarryOver.Outcome.CARRIED,
                carried={"review_verdicts": 1},
            )
            reviewer = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER, extra={"reviewed_sha": head})
            task = Task.objects.create(
                ticket=reviewer, session=Session.objects.create(ticket=reviewer), phase="reviewing"
            )
            with (
                patch("teatree.core.review.review_plan.matching_clone", return_value=repo),
                patch("teatree.core.review.review_plan.resolve_target_branch", return_value="main"),
                patch("teatree.core.review.live_head.live_head_at", return_value=LiveHeadRead.of(head)),
            ):
                plan = plan_for_task(task)

            assert plan is not None
            assert plan.delta.prior_head == reviewed
            assert "+fixed" in plan.delta.patch

    def test_stored_patch_is_bounded_without_changing_line_metric(self) -> None:
        head = "9" * 40
        reviewer = Ticket.objects.create(
            issue_url="https://github.com/example/repo/pull/8", role=Ticket.Role.REVIEWER, extra={"reviewed_sha": head}
        )
        task = Task.objects.create(ticket=reviewer, session=Session.objects.create(ticket=reviewer), phase="reviewing")
        with (
            patch("teatree.core.review.review_plan.matching_clone", return_value=Path("/tmp")),
            patch("teatree.core.review.review_plan.resolve_target_branch", return_value="main"),
            patch("teatree.core.review.live_head.live_head_at", return_value=LiveHeadRead.of(head)),
            patch(
                "teatree.core.review.review_plan.resolve_delta",
                return_value=ReviewDelta("delta", prior_head="a" * 40, patch="+x\n" * 10_000),
            ),
        ):
            bounded = plan_for_task(task)
        assert bounded is not None
        metric = RoundMetric.objects.get(ticket=reviewer, kind="review", subject=head)
        assert metric.size == 10_000
        assert len(metric.detail["patch"]) <= 24_001
        assert "diff truncated" in "\n".join(bounded.brief_lines())

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
                for index in range(4)
            ]
            for criterion, rationale in zip(
                criteria,
                ("untouched.py:1", "touched.py:2", "uncited rationale", "tests/unchanged.py:1"),
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
            TaskAttempt.objects.create(
                task=task, result={"review_verdict": {"reviewed_sha": prior}, "decisions": ["keep the API"]}
            )

            with (
                patch("teatree.core.review.review_plan.matching_clone", return_value=repo),
                patch("teatree.core.review.review_plan.resolve_target_branch", return_value="main"),
                patch("teatree.core.review.live_head.live_head_at", return_value=LiveHeadRead.of(head)),
            ):
                plan = plan_for_task(task)
                repeated = plan_for_task(task)

            assert plan is not None
            assert plan.delta.case == "delta"
            assert plan.carried == frozenset({0, 3})
            assert "CARRIED: #0, #3" in "\n".join(plan.brief_lines())
            assert "RE-GRADE: #1, #2" in "\n".join(plan.brief_lines())
            assert "fix the branch" in "\n".join(plan.brief_lines())
            assert "Prior decisions:\n  keep the API" in "\n".join(plan.brief_lines())
            assert repeated == plan
            metric = RoundMetric.objects.get(ticket=owner, kind="review", subject=head)
            assert metric.size == plan.delta.changed_lines

            _assert_fallbacks(task, repo, prior, head)

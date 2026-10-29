from teatree.core.models import ReviewVerdict, Task
from teatree.core.models.review_target import review_target_for_task


def complete_review_task(task: Task) -> None:
    target = review_target_for_task(task)
    assert target is not None
    ReviewVerdict.objects.create(
        ticket=task.ticket,
        slug=target.slug,
        pr_id=target.pr_id,
        reviewed_sha=target.head_sha,
        verdict=ReviewVerdict.Verdict.MERGE_SAFE,
        reviewer_identity="fixture-reviewer",
        blast_class="logic",
        gh_verify_result="green",
    )
    task.complete_with_attempt(result={"review_verdict": {"verdict": "merge_safe"}})

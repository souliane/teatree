from teatree.core.review.pr_next_action import PrActionFacts, ci_label, decide
from teatree.loop.scanners.pr_sweep_reporting import inventory_entry
from teatree.loop.scanners.pr_sweep_types import MergeAttempt
from tests.teatree_loop.test_pr_sweep_scanner import _open_pr


def test_failing_required_checks_are_red_even_when_the_reason_is_review_related() -> None:
    assert ci_label("mergeable_awaiting_review", failing_required=True) == "red"
    assert ci_label("mergeable_awaiting_review", failing_required=False) == "green"


def test_critic_wait_and_review_cap_have_actionable_inventory_rows() -> None:
    pr = _open_pr(owned=False)
    for decision, reason, expected_group, expected_task in (
        ("waiting_on_critic", "critic running", "Factory working", "Wait for the merge-quality critic"),
        ("flag_no_review", "review_rounds_exhausted", "Needs you", "Inspect three recorded review rounds"),
    ):
        attempt = MergeAttempt(slug=pr.slug, pr_id=pr.number, decision=decision, reason=reason, url=pr.url)
        entry = inventory_entry(pr, attempt)
        facts = PrActionFacts(
            entry=entry,
            working="",
            failure="",
            review_requested=False,
            question="",
            ci=entry.ci,
            approvals=entry.approvals,
            threads=entry.threads,
            threads_blocking=False,
            draft=False,
            verified=True,
            owned=True,
        )
        assert entry.ci == "green"
        assert entry.next_task == expected_task
        assert decide(facts).group == expected_group

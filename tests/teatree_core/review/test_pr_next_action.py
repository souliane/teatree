from teatree.core.review.pr_next_action import ci_label


def test_failing_required_checks_are_red_even_when_the_reason_is_review_related() -> None:
    assert ci_label("mergeable_awaiting_review", failing_required=True) == "red"
    assert ci_label("mergeable_awaiting_review", failing_required=False) == "green"

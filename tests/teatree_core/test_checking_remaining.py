from teatree.core.checking_remaining import remaining_steps
from teatree.core.models.ticket import Ticket


def test_pre_pr_ladder_starts_after_current_state() -> None:
    assert remaining_steps(Ticket.State.CODED, "", has_pr=False, merge_safe=False)[:3] == (
        "testing",
        "factory review",
        "shipping",
    )


def test_pr_ladder_reopens_rework_and_respects_merge_safe() -> None:
    assert remaining_steps(Ticket.State.PR_OPENED, "coding", has_pr=True, merge_safe=True) == (
        "rework",
        "testing",
        "factory review",
        "independent review",
        "merge",
        "deploy",
    )
    assert remaining_steps(Ticket.State.REVIEW_REQUESTED, "", has_pr=True, merge_safe=True) == ("merge", "deploy")


def test_review_variants_and_merged_work_keep_their_current_step() -> None:
    for phase in ("codex_reviewing", "codex_adversarial_reviewing", "e2e_reviewing", "critic_reviewing"):
        assert remaining_steps(Ticket.State.PR_OPENED, phase, has_pr=True, merge_safe=False)[:2] == (
            "factory review",
            "independent review",
        )
    assert remaining_steps(Ticket.State.MERGED, "coding", has_pr=False, merge_safe=False) == ("deploy",)
    assert remaining_steps(Ticket.State.RETRO_RECORDED, "coding", has_pr=False, merge_safe=False) == ("deploy",)

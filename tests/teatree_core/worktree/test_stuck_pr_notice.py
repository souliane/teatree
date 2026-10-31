import pytest

from teatree.core.fleet.wire import ClaimLoss
from teatree.core.worktree.stuck_pr_notice import plain_failure_cause

FALLBACK = "The last attempt could not open the pull request."

CAUSES = {
    ClaimLoss.STOLEN: "Another run now holds the claim on this branch's work.",
    ClaimLoss.UNREADABLE: "The claim on this branch's work cannot be read, so its holder is unknown.",
    ClaimLoss.MISSING: "The claim this run held on the branch's work no longer exists.",
    ClaimLoss.UNPROVEN: "This run cannot prove the claim on the branch's work is still its own.",
    ClaimLoss.UNREACHABLE: "The place that records the claim on this branch's work is unreachable.",
}


@pytest.mark.parametrize("loss", list(ClaimLoss))
def test_each_claim_loss_gets_its_own_plain_cause(loss: ClaimLoss) -> None:
    assert plain_failure_cause(f"Refusing to ship: {loss}.") == CAUSES[loss]


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        ("branch not on remote yet", "The branch has not reached its remote yet."),
        (
            "refusing to ship: merging origin/main into 'feat/x' produced conflicts in: a.py.",
            "The branch conflicts with its target.",
        ),
        (
            "debt_delta_gate: this ship introduces 1 net-new tech-debt suppression(s) with no plan-manifest waiver",
            "A shipping gate is blocking the pull request.",
        ),
        (
            "max_open_prs_per_repo_per_ticket=2 for overlay 'x' would be exceeded: ticket T already has 2 open PR(s)",
            "A shipping gate is blocking the pull request.",
        ),
    ],
)
def test_known_refusals_get_a_plain_cause(error: str, cause: str) -> None:
    assert plain_failure_cause(error) == cause


@pytest.mark.parametrize("error", ["unknown: /secret/path", "the issue claim was refused", ""])
def test_cause_is_plain_and_does_not_replay_a_raw_error(error: str) -> None:
    assert plain_failure_cause(error) == FALLBACK

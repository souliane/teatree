"""The pure fulfil rule — all five evidence classes green, or REVERT (north-star PR-7).

Anti-vacuity in both directions: the all-green case fulfils, AND each of the five
classes is proven to flip the decision to REVERT independently — a merely-green
auto-change that fails any one class is not fulfilled.
"""

from teatree.loops.directive_loop.decide import VerifyEvidence, decide_fulfilment


def _green(**over: object) -> VerifyEvidence:
    base: dict[str, object] = {
        "activation_live": True,
        "acceptance_green": True,
        "probe_finding": "",
        "collateral_regression": "",
        "open_critic_findings": 0,
    }
    base.update(over)
    return VerifyEvidence(**base)


class TestDecideFulfilment:
    def test_all_five_green_fulfils(self) -> None:
        decision = decide_fulfilment(_green())
        assert decision.fulfilled is True

    def test_activation_not_live_reverts(self) -> None:
        decision = decide_fulfilment(_green(activation_live=False))
        assert decision.fulfilled is False
        assert "activation" in decision.reason

    def test_acceptance_red_reverts(self) -> None:
        decision = decide_fulfilment(_green(acceptance_green=False))
        assert decision.fulfilled is False
        assert "acceptance" in decision.reason

    def test_probe_violation_reverts(self) -> None:
        decision = decide_fulfilment(_green(probe_finding="ticket 7 has 2 open PRs"))
        assert decision.fulfilled is False
        assert "probe" in decision.reason

    def test_collateral_regression_reverts(self) -> None:
        decision = decide_fulfilment(_green(collateral_regression="review_catch"))
        assert decision.fulfilled is False
        assert "regression" in decision.reason

    def test_open_critic_finding_reverts(self) -> None:
        # A clean-but-green change that carries an open critic finding is NOT fulfilled.
        decision = decide_fulfilment(_green(open_critic_findings=1))
        assert decision.fulfilled is False
        assert "CriticFinding" in decision.reason

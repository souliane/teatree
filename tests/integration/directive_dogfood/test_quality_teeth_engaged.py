"""Test C — the quality teeth fire for directive work (north-star PR-8).

A ``setting_policy_gate`` directive rides the real maker pipeline: the loop anchors a
synthetic AUTHOR ticket, and the merge-quality tooth (PR-4) gates it UNCONDITIONALLY
while the debt-delta tooth (PR-3) is reachable per-overlay through the SAME resolver
the directive activation uses. This crosses the implement↔gate seam no unit suite
does: fail-closed at the exact shipped head, then satisfiable once a clean covering
verdict is recorded.

The admitted directive is built directly (no interpret dispatch — that is Test A's
job); Test C's subject is the teeth downstream of admission.
"""

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.gates.debt_delta_gate import DebtDeltaExceededError, check_debt_delta
from teatree.core.gates.merge_quality_gate import (
    MergeQualityVerdictError,
    assert_merge_quality_verdict,
    is_directive_ticket,
    merge_quality_enforced,
    ratified_test_strategy,
)
from teatree.core.models import ConfigSetting, CriticDispatch, CriticVerdict, DeferredQuestion, PullRequest, Task
from teatree.core.models.directive import Directive
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from teatree.quality.debt_delta import DebtWaiver
from tests.integration.directive_dogfood.exemplar import (
    ACCEPTANCE_NODE_ID,
    EXEMPLAR_ENVELOPE,
    PROOF_CASE_TEXT,
    SCOPE,
    enable_directive_loop_in_test_db,
    seed_critic_liveness,
    tick,
)

_HEAD = "c" * 40
_PR_REPO = "acme/mech"
_PR_IID = 7
_NEW_NOQA = (
    "diff --git a/src/teatree/m.py b/src/teatree/m.py\n"
    "--- a/src/teatree/m.py\n"
    "+++ b/src/teatree/m.py\n"
    "@@ -1,2 +1,3 @@\n"
    " unchanged = 1\n"
    "+risky = frobnicate()  # noqa: F821\n"
)


def _clean_merge_items() -> list[dict]:
    return [
        {"slug": "test_value", "status": "pass", "citation": f"the mechanism adds exactly {ACCEPTANCE_NODE_ID}"},
        {"slug": "cleanliness", "status": "pass", "citation": "fully typed, no smuggled Any, docs consistent"},
    ]


def _admitted_setting_policy_gate() -> Directive:
    raw = dict(EXEMPLAR_ENVELOPE["directive_interpretation"]["sketch"])
    raw.update(
        kind="setting_policy_gate",
        setting_key="dogfood_policy_flag",
        acceptance_tests=[ACCEPTANCE_NODE_ID],
        behavior_probe="",
        probe_none_reason="covered by acceptance tests",
    )
    directive = Directive.objects.capture(PROOF_CASE_TEXT, source=Directive.Source.CLI, scope_overlay=SCOPE)
    directive.record_interpretation(sketch_from_envelope(raw), constraint_statement="at most 1 open PR")
    question = DeferredQuestion.record("Ratify?", options_hash=f"directive_ratify:{directive.pk}")
    directive.attach_ratification(question)
    DeferredQuestion.consume(question.pk, answer="approve")
    directive.refresh_from_db()
    directive.admit()
    return directive


class TestQualityTeethEngaged(TestCase):
    def setUp(self) -> None:
        enable_directive_loop_in_test_db()
        seed_critic_liveness()

    def test_merge_and_debt_teeth_gate_the_loop_created_ticket(self) -> None:
        directive = _admitted_setting_policy_gate()

        # 1 — the loop anchors a real synthetic mechanism ticket + coding task + baseline.
        assert tick().action == "implementing"
        directive.refresh_from_db()
        ticket = directive.ticket
        assert ticket is not None
        assert ticket.extra["directive_id"] == directive.pk
        assert Task.objects.pending_in_phase("coding").filter(ticket=ticket).exists()
        assert directive.baseline_snapshot is not None
        assert directive.state == Directive.State.IMPLEMENTING

        # 2 — the implement↔merge-gate seam: a directive ticket is gated UNCONDITIONALLY.
        assert is_directive_ticket(ticket) is True
        assert merge_quality_enforced(ticket) is True
        assert ACCEPTANCE_NODE_ID in ratified_test_strategy(ticket)

        # 3 — the merge tooth is fail-closed then satisfiable at the EXACT shipped head.
        PullRequest.objects.create(
            ticket=ticket,
            overlay=SCOPE,
            url=f"https://github.com/{_PR_REPO}/pull/{_PR_IID}",
            repo=_PR_REPO,
            iid=str(_PR_IID),
        )
        with pytest.raises(MergeQualityVerdictError):
            assert_merge_quality_verdict(slug=_PR_REPO, pr_id=_PR_IID, head_sha=_HEAD)
        assert CriticDispatch.objects.filter(ticket=ticket, transition="merge").exists()
        CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="merge",
            head_sha=_HEAD,
            envelope={"grader_identity": "critic-agent-7", "items": _clean_merge_items()},
        )
        assert_merge_quality_verdict(slug=_PR_REPO, pr_id=_PR_IID, head_sha=_HEAD)  # now clean → no raise

        # 4 — the debt tooth flows through the SAME per-overlay resolver the activation uses.
        ConfigSetting.objects.set_value("require_debt_delta", value=True, scope=SCOPE)
        assert get_effective_settings(SCOPE).require_debt_delta is True
        with pytest.raises(DebtDeltaExceededError):
            check_debt_delta(ticket, _NEW_NOQA, waivers=())
        waiver = DebtWaiver(pattern="noqa: F821", reason="upstream stub gap, tracked separately")
        check_debt_delta(ticket, _NEW_NOQA, waivers=(waiver,))  # waived → no raise

        # 5 — fake-merge the mechanism ticket → the loop closes onto CONFIGURING.
        assert tick(merged_probe=lambda _d: True).action == "configuring"
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.CONFIGURING

"""Test B — keep-only-if-verified + instant rollback (north-star PR-8).

Drives the proof-case to VERIFYING, plants a real behaviour violation (two open PRs
for one ``(ticket, repo)`` in scope), then ticks past the horizon: the REAL probe
finds the breach, the directive parks in REVERT_PENDING, the overlay ``ConfigSetting``
is rolled back INSTANTLY (the resolver reads the neutral default again), and the
human-consumed revert drives it to terminal REVERTED. The ``acceptance_reader`` is
the one injected seam — the real acceptance subprocess is Test A's job, run once.
"""

from datetime import timedelta

from django.test import TestCase

from teatree.core.gates.pr_budget_gate import resolve_pr_budget
from teatree.core.models import PullRequest, Ticket
from teatree.core.models.directive import Directive
from teatree.loops.directive_loop.revert import resolve_revert
from teatree.loops.directive_loop.verify import VerifySeams
from tests.integration.directive_dogfood.exemplar import (
    SCOPE,
    drive_activation_only_to_verifying,
    enable_directive_loop_in_test_db,
    seed_critic_liveness,
    tick,
)

_SKIP_ACCEPTANCE = VerifySeams(acceptance_reader=lambda _d: True)


class TestRevertOnViolation(TestCase):
    def setUp(self) -> None:
        enable_directive_loop_in_test_db()
        seed_critic_liveness()

    def test_probe_violation_reverts_and_rolls_back_instantly(self) -> None:
        directive = drive_activation_only_to_verifying()
        assert directive.state == Directive.State.VERIFYING
        assert resolve_pr_budget(SCOPE) == 1

        # Plant the breach: two open PRs for one (ticket, repo) in scope — over the limit of 1.
        offender = Ticket.objects.create(issue_url="https://github.com/acme/repo-x/issues/9", overlay=SCOPE)
        for iid in ("1", "2"):
            PullRequest.objects.create(
                ticket=offender,
                overlay=SCOPE,
                url=f"https://github.com/acme/repo-x/pull/{iid}",
                repo="acme/repo-x",
                iid=iid,
            )

        past_horizon = directive.verify_started_at + timedelta(days=8)
        assert tick(now=past_horizon, verify_seams=_SKIP_ACCEPTANCE).action == "revert_pending"
        directive.refresh_from_db()
        assert directive.state == Directive.State.REVERT_PENDING
        assert "acme/repo-x" in directive.decision_reason
        assert "limit 1" in directive.decision_reason
        # Instant rollback: the ConfigSetting row is gone, so the resolver reads the neutral default.
        assert resolve_pr_budget(SCOPE) == 0

        # The revert is human-ratified: the next tick asks, then the operator close-out
        # (`t3 directive resolve-revert`) consumes the question and reaches terminal REVERTED.
        assert tick(now=past_horizon).action == "revert_asked"
        directive.refresh_from_db()
        resolve_revert(directive)
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.REVERTED
        assert resolve_pr_budget(SCOPE) == 0

"""The dispatch chokepoint refuses an implementing agent on an already-closed issue (#2663).

Mirrors ``test_runner_plan_dispatch_gate.py``: the gate's value is that it fires at
the seam a dispatch cannot avoid, so this drives ``run_agent`` end to end and asserts
the harness never opened — a gate that refuses only after the child is spawned has
already paid for the run it refused.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.agents import harness as harness_mod
from teatree.agents import runner as runner_mod
from teatree.agents.runner import run_agent
from teatree.core.gates.closed_issue_dispatch_gate import ISSUE_CLOSED_PREFIX
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from tests.teatree_agents._sdk_fake import FakeHarnessSession, success_stream

_URL = "https://github.com/souliane/teatree/issues/4045"


class _Host:
    """Code-host stub: answers ``get_issue`` with one payload, or raises."""

    def __init__(self, payload: object = None, *, raises: bool = False) -> None:
        self.payload = payload
        self.raises = raises
        self.calls: list[str] = []

    def get_issue(self, issue_url: str) -> object:
        self.calls.append(issue_url)
        if self.raises:
            msg = "connection timeout"
            raise RuntimeError(msg)
        return self.payload


class _Provider:
    def __init__(self, host: _Host | None) -> None:
        self.host = host

    def get_code_host_for_url(self, overlay: object, issue_url: str) -> _Host | None:
        _ = (overlay, issue_url)
        return self.host


class _DispatchProbe(TestCase):
    """Drives ``run_agent`` with the SDK child replaced by a spy that records spawns."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url=_URL)
        # The plan gate sits ahead of this one at the same seam, so an unplanned
        # ticket would refuse for the WRONG reason and the assertions would pass
        # while proving nothing about the closed-issue gate.
        PlanArtifact.record(ticket=self.ticket, plan_text="Do X by Y", recorded_by="t3:planner")
        self.spawned: list[object] = []

    def _dispatch(self, phase: str, host: _Host | None, *, ticket: Ticket | None = None) -> Task:
        def _make_client(*, options: object = None, **_: object) -> FakeHarnessSession:
            self.spawned.append(options)
            return FakeHarnessSession(success_stream({"summary": "ok"}))

        session = Session.objects.create(ticket=ticket or self.ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket or self.ticket, session=session, phase=phase)
        snapshot = runner_mod.TaskUsage(turns=0, cost_usd=0.0)
        with (
            patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(harness_mod, "ClaudeSDKClient", _make_client),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
            patch("teatree.core.backend_registry.get_backend_provider", return_value=_Provider(host)),
            patch("teatree.core.overlay_loader.get_overlay_for_ticket", return_value=object()),
        ):
            run_agent(task, phase=phase, overlay_skill_metadata={})
        task.refresh_from_db()
        return task


class TestClosedIssueImplementingDispatchIsRefused(_DispatchProbe):
    def test_coding_dispatch_is_refused_before_the_harness_opens(self) -> None:
        task = self._dispatch("coding", _Host({"state": "closed", "state_reason": "not_planned"}))

        assert self.spawned == [], "no agent child may be spawned against a closed issue"
        assert task.status == Task.Status.FAILED
        attempt = task.attempts.order_by("-pk").first()
        assert attempt is not None
        assert attempt.error.startswith(ISSUE_CLOSED_PREFIX)
        assert "not_planned" in attempt.error

    def test_a_testing_dispatch_is_refused_and_bills_no_spend(self) -> None:
        task = self._dispatch("testing", _Host({"state": "closed"}))

        assert self.spawned == []
        attempt = task.attempts.order_by("-pk").first()
        assert attempt is not None
        assert attempt.error.startswith(ISSUE_CLOSED_PREFIX)
        # Refused before the harness opened, so no turn was billed — NULL, not zero.
        assert attempt.cost_usd is None

    def test_the_refusal_is_recorded_under_its_own_failure_kind(self) -> None:
        # The name is what `stuck_ticket_redispatch` reads to HALT rather than churn.
        task = self._dispatch("coding", _Host({"state": "closed"}))

        assert task.failure_kind == "issue_closed"


class TestAnOpenIssueDispatchesNormally(_DispatchProbe):
    def test_an_open_issue_lets_the_coder_spawn(self) -> None:
        self._dispatch("coding", _Host({"state": "open"}))

        assert self.spawned, "an open issue must still dispatch its coder"

    def test_a_reopened_issue_lets_the_coder_spawn(self) -> None:
        self._dispatch("coding", _Host({"state": "open", "state_reason": "reopened"}))

        assert self.spawned, "a reopened issue is live work"


class TestTheGateFailsOpen(_DispatchProbe):
    def test_a_raising_host_lets_the_coder_spawn(self) -> None:
        # A forge outage must not freeze the factory: the disposition scan retries
        # every tick, so blocking here would trade an outage for a stalled board.
        self._dispatch("coding", _Host(raises=True))

        assert self.spawned, "an unreadable issue must not block the dispatch"

    def test_an_unresolvable_host_lets_the_coder_spawn(self) -> None:
        self._dispatch("coding", None)

        assert self.spawned, "a ticket whose host does not resolve must not be blocked"

    def test_an_error_payload_lets_the_coder_spawn(self) -> None:
        self._dispatch("coding", _Host({"error": "Not a GitHub issue URL"}))

        assert self.spawned, "an error envelope is not a closed issue"


class TestOutOfScopeDispatchesAreUntouched(_DispatchProbe):
    def test_a_planner_spawns_against_a_closed_issue(self) -> None:
        # Planning a closed issue is how an operator decides what to do with it;
        # gating the read-only phases would refuse the very work that resolves this.
        host = _Host({"state": "closed"})
        self._dispatch("planning", host)

        assert self.spawned, "a non-implementing dispatch is never gated on issue state"
        assert host.calls == [], "the gate must not pay a forge round trip it cannot act on"

    def test_a_reviewer_ticket_spawns_against_a_closed_url(self) -> None:
        reviewer = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            issue_url="https://github.com/souliane/teatree/pull/4200",
        )
        PlanArtifact.record(ticket=reviewer, plan_text="Review it", recorded_by="t3:planner")
        host = _Host({"state": "closed"})
        self._dispatch("coding", host, ticket=reviewer)

        assert self.spawned, "a reviewer ticket's URL is somebody else's PR, not its own issue"
        assert host.calls == []

    def test_a_synthetic_cadence_url_spawns_without_a_forge_read(self) -> None:
        cadence = Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url="scanning-news://t3-teatree")
        PlanArtifact.record(ticket=cadence, plan_text="Scan", recorded_by="t3:planner")
        host = _Host({"state": "closed"})
        self._dispatch("coding", host, ticket=cadence)

        assert self.spawned, "a loop-cadence anchor has no forge issue to be closed"
        assert host.calls == []

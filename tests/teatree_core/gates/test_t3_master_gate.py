"""teatree.core.gates.t3_master_gate — the shared t3-master gate the reactive loops consult (#3968).

Every arc of the three-outcome verdict, plus the two skip messages that must be
distinguishable: "nothing owns this" and "another live session owns this" call for
opposite operator responses, and the pre-#3968 wording ("this session is not the loop
owner") conflated them.
"""

import os

import pytest

from teatree.core.gates.t3_master_gate import T3MasterGate, live_foreign_owner_session, t3_master_verdict
from teatree.core.loop_lease_manager import T3_MASTER_SLOT
from teatree.core.models import LoopLease
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID
from tests._loop_principal_env import pinned_loop_principal

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _claim(session_id: str, *, owner_pid: int | None) -> None:
    LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id=session_id, owner_pid=owner_pid)


class TestVerdict:
    def test_unclaimed_slot_does_not_run(self) -> None:
        verdict = t3_master_verdict()

        assert verdict.outcome is T3MasterGate.UNCLAIMED
        assert verdict.may_run is False
        assert verdict.owner_session == ""

    def test_released_slot_does_not_run(self) -> None:
        _claim(LOOP_RUNNER_SESSION_ID, owner_pid=os.getpid())
        LoopLease.objects.release_ownership(T3_MASTER_SLOT, session_id=LOOP_RUNNER_SESSION_ID)

        assert t3_master_verdict().outcome is T3MasterGate.UNCLAIMED

    def test_loop_runner_owner_runs_for_any_caller(self) -> None:
        _claim(LOOP_RUNNER_SESSION_ID, owner_pid=os.getpid())

        verdict = t3_master_verdict(caller_session="some-other-session")

        assert verdict.outcome is T3MasterGate.RUN
        assert verdict.may_run is True
        assert verdict.owner_session == LOOP_RUNNER_SESSION_ID

    def test_own_session_owner_runs(self) -> None:
        _claim("sess-1", owner_pid=os.getpid())

        assert t3_master_verdict(caller_session="sess-1").outcome is T3MasterGate.RUN

    def test_foreign_live_session_blocks_and_names_the_owner(self) -> None:
        _claim("sess-other", owner_pid=os.getpid())

        verdict = t3_master_verdict(caller_session="sess-mine")

        assert verdict.outcome is T3MasterGate.FOREIGN_OWNER
        assert verdict.may_run is False
        assert verdict.owner_session == "sess-other"

    def test_anonymous_caller_blocked_by_a_foreign_live_session(self) -> None:
        _claim("sess-other", owner_pid=os.getpid())

        assert t3_master_verdict(caller_session="").outcome is T3MasterGate.FOREIGN_OWNER

    def test_caller_session_defaults_to_the_loop_principal(self) -> None:
        _claim("sess-env", owner_pid=os.getpid())

        with pinned_loop_principal("sess-env"):
            assert t3_master_verdict().outcome is T3MasterGate.RUN

    def test_default_caller_is_blocked_by_a_foreign_principal(self) -> None:
        _claim("sess-other", owner_pid=os.getpid())

        with pinned_loop_principal("sess-mine"):
            assert t3_master_verdict().outcome is T3MasterGate.FOREIGN_OWNER


class TestSkipMessage:
    def test_unclaimed_and_foreign_messages_differ(self) -> None:
        unclaimed = t3_master_verdict().skip_message("self-improve")
        _claim("sess-other", owner_pid=os.getpid())
        foreign = t3_master_verdict(caller_session="sess-mine").skip_message("self-improve")

        assert unclaimed != foreign
        assert "self-improve" in unclaimed
        assert "self-improve" in foreign

    def test_unclaimed_message_names_the_absent_owner_and_the_remedy(self) -> None:
        message = t3_master_verdict().skip_message("Slack-answer")

        assert "no live owner" in message
        assert "t3 worker" in message

    def test_foreign_message_names_the_owning_session(self) -> None:
        _claim("sess-other", owner_pid=os.getpid())

        message = t3_master_verdict(caller_session="sess-mine").skip_message("Slack-answer")

        assert "sess-other" in message
        assert "another live session" in message

    def test_running_verdict_has_no_skip_message(self) -> None:
        _claim(LOOP_RUNNER_SESSION_ID, owner_pid=os.getpid())

        assert t3_master_verdict().skip_message("Slack-answer") == ""


class TestLiveForeignOwnerSession:
    """The SessionStart tick-owner election's read — the runner is not a rival."""

    def test_runner_owned_slot_reports_no_foreign_owner(self) -> None:
        _claim(LOOP_RUNNER_SESSION_ID, owner_pid=os.getpid())

        assert live_foreign_owner_session("a-session", current_pid=os.getpid() + 1) == ""

    def test_a_live_foreign_session_is_still_reported(self) -> None:
        _claim("live-peer", owner_pid=os.getpid())

        assert live_foreign_owner_session("a-session", current_pid=os.getpid() + 1) == "live-peer"

    def test_unclaimed_slot_reports_no_foreign_owner(self) -> None:
        assert live_foreign_owner_session("a-session", current_pid=os.getpid() + 1) == ""

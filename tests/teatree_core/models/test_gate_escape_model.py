from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.escape_scope import EscapeScope
from teatree.core.models.gate_escape import GateEscape, GateEscapeError
from tests._gate_escape_audit import NEVER_ESCAPABLE_GATES


def _scope(**changes: str) -> EscapeScope:
    fields = {
        "gate": "main_clone",
        "reason": "fixture",
        "actor": "coding-agent",
        "subject": "/work/repo",
        "head": "a" * 40,
        "action_digest": "b" * 64,
        "session_id": "session-1",
        "call_id": "call-1",
    }
    fields.update(changes)
    return EscapeScope(**fields)


class TestGateEscape(TestCase):
    def test_binding_tracks_action_scope_but_not_retry_call_id(self) -> None:
        assert _scope().binding() == _scope(call_id="retry", reason="new reason").binding()
        assert _scope().binding() != _scope(head="c" * 40).binding()

    def test_agent_escape_records_before_action_and_refuses_replay(self) -> None:
        scope = _scope()
        row = GateEscape.objects.record_agent(scope)
        assert row.recorded_at is not None
        assert row.issuer_kind == GateEscape.Issuer.AGENT
        with pytest.raises(GateEscapeError):
            GateEscape.objects.record_agent(scope)
        next_call = GateEscape.objects.record_agent(_scope(call_id="call-2"))
        assert next_call.call_id == "call-2"
        assert GateEscape.objects.count() == 2

    def test_human_grant_is_single_use(self) -> None:
        scope = _scope(gate="raw_review_post")
        grant = GateEscape.objects.grant_human(scope, issuer_id="owner", ttl=timedelta(minutes=5))
        assert grant.recorded_at is None
        used = GateEscape.objects.consume_human(scope)
        assert used is not None
        assert used.recorded_at is not None
        assert GateEscape.objects.consume_human(scope) is None

    def _assert_grant_refuses_changed_field(self, field: str, replacement: str) -> None:
        scope = _scope(gate="raw_review_post")
        GateEscape.objects.grant_human(scope, issuer_id="owner", ttl=timedelta(minutes=5))
        assert GateEscape.objects.consume_human(_scope(**{"gate": "raw_review_post", field: replacement})) is None
        assert GateEscape.objects.get().recorded_at is None
        assert GateEscape.objects.consume_human(scope) is not None

    def test_human_grant_is_bound_to_gate(self) -> None:
        self._assert_grant_refuses_changed_field("gate", "raw_issue_write")

    def test_human_grant_is_bound_to_actor(self) -> None:
        self._assert_grant_refuses_changed_field("actor", "other-agent")

    def test_human_grant_is_bound_to_subject(self) -> None:
        self._assert_grant_refuses_changed_field("subject", "/work/other")

    def test_human_grant_is_bound_to_head(self) -> None:
        self._assert_grant_refuses_changed_field("head", "c" * 40)

    def test_human_grant_is_bound_to_action_digest(self) -> None:
        self._assert_grant_refuses_changed_field("action_digest", "d" * 64)

    def test_human_grant_is_bound_to_session_id(self) -> None:
        self._assert_grant_refuses_changed_field("session_id", "session-2")

    def test_expired_grant_refuses_without_an_audit_write(self) -> None:
        scope = _scope(gate="raw_review_post")
        grant = GateEscape.objects.grant_human(scope, issuer_id="owner", ttl=timedelta(minutes=5))
        GateEscape.objects.filter(pk=grant.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        assert GateEscape.objects.consume_human(scope) is None
        grant.refresh_from_db()
        assert grant.recorded_at is None

    def test_human_grant_refuses_a_maker_role_issuer(self) -> None:
        with pytest.raises(GateEscapeError):
            GateEscape.objects.grant_human(_scope(gate="raw_review_post"), issuer_id="maker", ttl=timedelta(minutes=5))
        assert GateEscape.objects.count() == 0

    def test_human_grant_refuses_the_acting_agent_as_issuer(self) -> None:
        scope = _scope(gate="raw_review_post", actor="session-agent-7")
        with pytest.raises(GateEscapeError):
            GateEscape.objects.grant_human(scope, issuer_id="session-agent-7", ttl=timedelta(minutes=5))
        assert GateEscape.objects.count() == 0

    def test_never_policy_refuses_agent_and_human_escapes_for_every_gate(self) -> None:
        for gate in NEVER_ESCAPABLE_GATES:
            with self.subTest(gate):
                with pytest.raises(GateEscapeError):
                    GateEscape.objects.record_agent(_scope(gate=gate))
                with pytest.raises(GateEscapeError):
                    GateEscape.objects.grant_human(_scope(gate=gate), issuer_id="owner", ttl=timedelta(minutes=5))
        assert GateEscape.objects.count() == 0

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.gate_escape import EscapeScope, GateEscape, GateEscapeError


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
        row = GateEscape.record_agent(scope)
        assert row.recorded_at is not None
        assert row.issuer_kind == GateEscape.Issuer.AGENT
        with pytest.raises(GateEscapeError):
            GateEscape.record_agent(scope)
        next_call = GateEscape.record_agent(_scope(call_id="call-2"))
        assert next_call.call_id == "call-2"
        assert GateEscape.objects.count() == 2

    def test_human_grant_is_scoped_and_single_use(self) -> None:
        scope = _scope(gate="raw_review_post")
        grant = GateEscape.grant_human(scope, issuer_id="owner", ttl=timedelta(minutes=5))
        assert grant.recorded_at is None
        assert GateEscape.consume_human(_scope(gate="raw_review_post", head="c" * 40)) is None
        used = GateEscape.consume_human(scope)
        assert used is not None
        assert used.recorded_at is not None
        assert GateEscape.consume_human(scope) is None

    def test_expired_grant_refuses_without_an_audit_write(self) -> None:
        scope = _scope(gate="raw_review_post")
        grant = GateEscape.grant_human(scope, issuer_id="owner", ttl=timedelta(minutes=5))
        GateEscape.objects.filter(pk=grant.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        assert GateEscape.consume_human(scope) is None
        grant.refresh_from_db()
        assert grant.recorded_at is None

    def test_human_grant_refuses_agent_or_actor_issuer(self) -> None:
        scope = _scope(gate="raw_review_post")
        for issuer in ("maker", "coding-agent"):
            with pytest.raises(GateEscapeError):
                GateEscape.grant_human(scope, issuer_id=issuer, ttl=timedelta(minutes=5))
        assert GateEscape.objects.count() == 0

import json
from datetime import timedelta
from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli import app
from teatree.core.modelkit.escape_scope import EscapeScope
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.gate_escape import GateEscape
from tests._gate_escape_audit import NEVER_ESCAPABLE_GATES

runner = CliRunner()


def _payload(gate: str = "main_clone") -> str:
    return json.dumps(
        {
            "gate": gate,
            "reason": "fixture",
            "actor": "coding-agent",
            "subject": "/work/repo",
            "head": "a" * 40,
            "action_digest": "b" * 64,
            "session_id": "session-1",
            "call_id": "call-1",
            "excerpt": "git status",
        }
    )


class TestGateEscapeCli(TestCase):
    def test_records_before_allow_and_refuses_replay(self) -> None:
        first = runner.invoke(app, ["tool", "gate-escape"], input=_payload())
        assert first.exit_code == 0
        assert json.loads(first.stdout)["allowed"] is True
        assert GateEscape.objects.count() == 1
        second = runner.invoke(app, ["tool", "gate-escape"], input=_payload())
        assert second.exit_code != 0
        assert json.loads(second.stdout)["allowed"] is False
        assert GateEscape.objects.count() == 1

    def test_refuses_human_only_and_never(self) -> None:
        for gate in ("raw_review_post", "quote_scanner"):
            result = runner.invoke(app, ["tool", "gate-escape"], input=_payload(gate))
            assert result.exit_code != 0
            assert json.loads(result.stdout)["allowed"] is False
        assert GateEscape.objects.count() == 0
        assert DeferredQuestion.objects.count() == 1

    def test_refuses_every_never_escapable_gate_without_a_row_or_a_card(self) -> None:
        for gate in NEVER_ESCAPABLE_GATES:
            with self.subTest(gate):
                result = runner.invoke(app, ["tool", "gate-escape"], input=_payload(gate))
                assert result.exit_code != 0
                assert json.loads(result.stdout)["allowed"] is False
        assert GateEscape.objects.count() == 0
        assert DeferredQuestion.objects.count() == 0

    def test_verified_human_grant_allows_once(self) -> None:
        scope = EscapeScope(**json.loads(_payload("raw_review_post")))
        GateEscape.objects.grant_human(scope, issuer_id="slack:U0DEMOOWNER", ttl=timedelta(hours=1))
        result = runner.invoke(app, ["tool", "gate-escape"], input=_payload("raw_review_post"))
        assert result.exit_code == 0
        assert json.loads(result.stdout)["allowed"] is True
        grant = GateEscape.objects.get()
        assert grant.recorded_at is not None
        replay = runner.invoke(app, ["tool", "gate-escape"], input=_payload("raw_review_post"))
        assert replay.exit_code != 0

    def test_refuses_unrecordable_action(self) -> None:
        with patch.object(GateEscape.objects, "record_agent", side_effect=OperationalError("locked")):
            result = runner.invoke(app, ["tool", "gate-escape"], input=_payload())
        assert result.exit_code != 0
        assert json.loads(result.stdout)["allowed"] is False
        assert GateEscape.objects.count() == 0

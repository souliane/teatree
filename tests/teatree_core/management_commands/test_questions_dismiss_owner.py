from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.modelkit.owner_action import OwnerActionKind
from teatree.core.modelkit.owner_decision import HELD_PR_MARKER_PREFIX, OwnerDecision
from teatree.core.models import Session, Ticket
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from tests._owner_channel import owner_card
from tests.factories import planned_ticket


def _dismiss(
    *ids: int, owner: bool = False, dispatched: bool = False, resolver: str = "", agent_env: str = ""
) -> tuple[int, str]:
    stderr = StringIO()
    environment = {"T3_DISPATCHED_AGENT": "1"} if dispatched else {}
    if agent_env:
        environment[agent_env] = "agent-session"
    with (
        patch("teatree.core.management.commands.owner_action_auth.sys.stdin.isatty", return_value=owner),
        patch.dict("os.environ", environment, clear=True),
    ):
        try:
            call_command("questions", "dismiss", *ids, "--resolver", resolver, stderr=stderr)
        except SystemExit as exc:
            return int(exc.code or 0), stderr.getvalue()
    return 0, stderr.getvalue()


class TestOwnerDismissal(TestCase):
    def _card(self, decision: OwnerDecision) -> DeferredQuestion:
        return DeferredQuestion.objects.create(
            question="Proceed?",
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            evidence=owner_card(decision).to_evidence(),
        )

    def test_agent_cannot_dismiss_any_owner_card(self) -> None:
        for decision in OwnerDecision:
            for resolver in ("", "mcp", "agent:interactive", "owner-terminal"):
                row = self._card(decision)
                code, error = _dismiss(row.pk, dispatched=True, resolver=resolver)
                assert code == 2
                assert str(row.pk) in error
                row.refresh_from_db()
                assert row.is_pending
                assert not DeferredQuestionAudit.objects.filter(question=row).exists()

    def test_attended_agent_cannot_dismiss_hard_limit_card(self) -> None:
        for decision in list(OwnerDecision)[:4]:
            row = self._card(decision)
            code, _error = _dismiss(row.pk, resolver="owner-terminal")
            assert code == 2
            assert DeferredQuestion.objects.get(pk=row.pk).is_pending

    def test_direct_owner_can_dismiss_kept_card(self) -> None:
        row = self._card(OwnerDecision.CREDENTIALS)
        code, _error = _dismiss(row.pk, owner=True)
        assert code == 0
        assert DeferredQuestion.objects.get(pk=row.pk).status == "dismissed"
        assert DeferredQuestionAudit.objects.filter(question=row, action="dismissed").exists()

    def test_attended_and_codex_session_markers_cannot_claim_direct_owner_authority(self) -> None:
        for agent_env in ("CLAUDECODE", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "T3_LOOP_SESSION_ID"):
            with self.subTest(agent_env=agent_env):
                row = self._card(OwnerDecision.CREDENTIALS)
                code, error = _dismiss(row.pk, owner=True, agent_env=agent_env)
                assert code == 2
                assert "credentials" in error
                assert DeferredQuestion.objects.get(pk=row.pk).is_pending

    def test_mixed_batch_dismisses_allowed_cards_and_names_refused_ids(self) -> None:
        kept = self._card(OwnerDecision.CREDENTIALS)
        allowed = self._card(OwnerDecision.ARCHITECTURE)
        code, error = _dismiss(kept.pk, allowed.pk)
        assert code == 0
        assert str(kept.pk) in error
        assert "credentials" in error
        assert DeferredQuestion.objects.get(pk=kept.pk).is_pending
        assert DeferredQuestion.objects.get(pk=allowed.pk).status == "dismissed"

    def test_unknown_and_held_owner_cards_fail_closed(self) -> None:
        legacy = DeferredQuestion.objects.create(question="Unknown?", audience="owner_question", evidence={})
        cardless_attest = DeferredQuestion.objects.create(
            question="Attest?",
            audience="owner_question",
            evidence={"action": {"kind": "attest", "target": "checkout", "binding": "head"}},
        )
        held = self._card(OwnerDecision.ARCHITECTURE)
        held.dedupe_marker = f"{HELD_PR_MARKER_PREFIX}subject:head"
        held.save(update_fields=["dedupe_marker"])

        code, error = _dismiss(legacy.pk, cardless_attest.pk, held.pk)

        assert code == 2
        assert str(legacy.pk) in error
        assert str(cardless_attest.pk) in error
        assert str(held.pk) in error
        assert (
            DeferredQuestion.objects.filter(
                pk__in=[legacy.pk, cardless_attest.pk, held.pk], dismissed_at__isnull=True
            ).count()
            == 3
        )

    def test_escape_and_void_action_cards_cannot_be_dismissed_by_an_agent(self) -> None:
        for action in OwnerActionKind.ESCAPE, OwnerActionKind.VOID:
            with self.subTest(action=action):
                row = self._card(OwnerDecision.IRREVERSIBLE)
                row.evidence = {**row.evidence, "action": {"kind": action.value, "target": "one", "binding": "head"}}
                row.save(update_fields=["evidence"])
                code, error = _dismiss(row.pk, dispatched=True)
                assert code == 2
                assert "irreversible" in error
                assert DeferredQuestion.objects.get(pk=row.pk).is_pending

    def test_finished_subject_remains_dismissible(self) -> None:
        ticket = planned_ticket()
        finished = self._card(OwnerDecision.CREDENTIALS)
        finished.task_session = Session.objects.create(ticket=ticket)
        finished.save(update_fields=["task_session"])
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.MERGED)
        code, _error = _dismiss(finished.pk, dispatched=True)

        assert code == 0
        assert DeferredQuestion.objects.filter(pk=finished.pk, dismissed_at__isnull=False).exists()

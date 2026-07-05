"""Wire-level persistence for the revived agent-dispatch zones (#1 blocker).

Each revived zone is exercised through the REAL ``dispatch() -> persist_agent_actions()``
round trip with a production-shaped ``ScanSignal`` — the previously-dropped zones
(codex review, red-card, red-MR fix, e2e-fix, answerer, skill-drift) must now
produce a ``Ticket`` + ``Task`` on the registered ``(role, phase)``, be
idempotent, and — for the marker-bearing zones — claim their idempotency marker
at PERSIST time so a dropped/failed persist rolls the marker back for retry.

Anti-vacuity: on the pre-fix code every zone below except ``t3:reviewer`` /
``t3:orchestrator`` was dropped by ``_ZONE_HANDLERS`` (``logger.debug`` + skip),
so ``persist_agent_actions`` returned ``[]`` and created no rows — every
``created`` / ``Task.objects.filter(...)`` assertion here was RED before the fix.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import Task, Ticket
from teatree.core.models.codex_review_marker import CodexReviewMarker
from teatree.core.models.red_mr_fix_attempt import RedMrFixAttempt
from teatree.loop.dispatch import DispatchAction, dispatch
from teatree.loop.persistence import persist_agent_actions
from teatree.loop.scanners.base import ScanSignal


def _agent_actions(signal: ScanSignal) -> list[DispatchAction]:
    """Dispatch a signal and return only its ``kind="agent"`` actions."""
    return [a for a in dispatch([signal]) if a.kind == "agent"]


class TestDebugZoneRevived(TestCase):
    """``my_pr.failed`` → author ``debugging`` task + persist-time RedMrFixAttempt."""

    def _signal(self, *, pr_url: str = "https://example.com/o/r/pull/5", head_sha: str = "sha-red-1") -> ScanSignal:
        return ScanSignal(
            kind="my_pr.failed",
            summary=f"PR failed: {pr_url}",
            payload={"pr_url": pr_url, "head_sha": head_sha, "overlay": "acme"},
        )

    def test_creates_author_debugging_task(self) -> None:
        created = persist_agent_actions(_agent_actions(self._signal()))
        assert len(created) == 1
        task = created[0]
        assert task.phase == "debugging"
        assert task.ticket.role == Ticket.Role.AUTHOR
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE

    def test_claims_red_mr_fix_marker_at_persist_time(self) -> None:
        persist_agent_actions(_agent_actions(self._signal(pr_url="https://x/pr/9", head_sha="sha-9")))
        assert RedMrFixAttempt.objects.filter(pr_url="https://x/pr/9", head_sha="sha-9").count() == 1

    def test_idempotent_across_ticks_same_sha(self) -> None:
        actions = _agent_actions(self._signal(pr_url="https://x/pr/10", head_sha="sha-10"))
        first = persist_agent_actions(actions)
        second = persist_agent_actions(_agent_actions(self._signal(pr_url="https://x/pr/10", head_sha="sha-10")))
        assert len(first) == 1
        assert second == []
        assert Task.objects.filter(ticket__issue_url="https://x/pr/10", phase="debugging").count() == 1
        assert RedMrFixAttempt.objects.filter(pr_url="https://x/pr/10").count() == 1

    def test_role_conflict_does_not_burn_marker(self) -> None:
        # An existing REVIEWER ticket for the same url makes the handler drop
        # (role conflict) BEFORE the claim — the marker is never touched, so the
        # next tick retries once the conflict clears.
        Ticket.objects.create(issue_url="https://x/pr/11", overlay="acme", role=Ticket.Role.REVIEWER)
        created = persist_agent_actions(_agent_actions(self._signal(pr_url="https://x/pr/11", head_sha="sha-11")))
        assert created == []
        assert not RedMrFixAttempt.objects.filter(pr_url="https://x/pr/11").exists()

    def test_task_creation_failure_rolls_back_marker(self) -> None:
        # Force the Task write to fail AFTER the marker claim: the shared atomic
        # block must roll the RedMrFixAttempt row back so the dropped action does
        # not burn its idempotency marker (#1 blocker — markers survive for retry).
        with patch("teatree.loop.persistence._create_phase_task", side_effect=RuntimeError("boom")):
            errors: dict[str, str] = {}
            created = persist_agent_actions(
                _agent_actions(self._signal(pr_url="https://x/pr/12", head_sha="sha-12")),
                errors=errors,
            )
        assert created == []
        assert not RedMrFixAttempt.objects.filter(pr_url="https://x/pr/12").exists()
        assert "persist:t3:debug" in errors


class TestCodexReviewZoneRevived(TestCase):
    """``codex_review.dispatch`` → reviewer variant task + persist-time CodexReviewMarker."""

    def _signal(
        self,
        *,
        pr_url: str = "https://github.com/o/r/pull/7",
        pr_id: int = 7,
        head_sha: str = "codexsha1",
        variant: str = "codex:review",
    ) -> ScanSignal:
        return ScanSignal(
            kind="codex_review.dispatch",
            summary=f"codex review {pr_url}",
            payload={
                "slug": "o/r",
                "pr_id": pr_id,
                "head_sha": head_sha,
                "pr_url": pr_url,
                "variant": variant,
                "overlay": "acme",
                "title": "PR 7",
            },
        )

    def test_standard_variant_creates_codex_reviewing_task(self) -> None:
        created = persist_agent_actions(_agent_actions(self._signal()))
        assert len(created) == 1
        task = created[0]
        assert task.phase == "codex_reviewing"
        assert task.ticket.role == Ticket.Role.REVIEWER
        assert task.ticket.extra["codex_variant"] == "codex:review"

    def test_adversarial_variant_creates_adversarial_phase(self) -> None:
        created = persist_agent_actions(
            _agent_actions(
                self._signal(pr_url="https://github.com/o/r/pull/8", pr_id=8, variant="codex:adversarial-review")
            ),
        )
        assert len(created) == 1
        assert created[0].phase == "codex_adversarial_reviewing"

    def test_claims_codex_marker_at_persist_time(self) -> None:
        persist_agent_actions(_agent_actions(self._signal(pr_id=100, head_sha="csha-100")))
        assert CodexReviewMarker.objects.filter(slug="o/r", pr_id=100, head_sha="csha-100").count() == 1

    def test_idempotent_after_marker_claim_across_completed_task(self) -> None:
        # Prove the PERSIST-time marker (not just the open-task check) dedups: even
        # after the first review task completes, the same SHA does not re-dispatch.
        first = persist_agent_actions(_agent_actions(self._signal(pr_id=101, head_sha="csha-101")))
        assert len(first) == 1
        first[0].complete()
        second = persist_agent_actions(_agent_actions(self._signal(pr_id=101, head_sha="csha-101")))
        assert second == []
        assert (
            Task.objects.filter(ticket__issue_url="https://github.com/o/r/pull/7", phase="codex_reviewing").count() == 1
        )

    def test_role_conflict_does_not_burn_marker(self) -> None:
        Ticket.objects.create(issue_url="https://github.com/o/r/pull/13", overlay="acme", role=Ticket.Role.AUTHOR)
        created = persist_agent_actions(
            _agent_actions(self._signal(pr_url="https://github.com/o/r/pull/13", pr_id=13, head_sha="csha-13")),
        )
        assert created == []
        assert not CodexReviewMarker.objects.filter(slug="o/r", pr_id=13).exists()

    def test_task_creation_failure_rolls_back_marker(self) -> None:
        with patch("teatree.loop.persistence._create_phase_task", side_effect=RuntimeError("boom")):
            errors: dict[str, str] = {}
            created = persist_agent_actions(
                _agent_actions(self._signal(pr_url="https://github.com/o/r/pull/14", pr_id=14, head_sha="csha-14")),
                errors=errors,
            )
        assert created == []
        assert not CodexReviewMarker.objects.filter(slug="o/r", pr_id=14).exists()
        assert "persist:codex:review" in errors


class TestRedCardZoneRevived(TestCase):
    """``red_card.signal`` → author corrective ``coding`` task, row_id stamped."""

    def _signal(self, *, row_id: int = 42) -> ScanSignal:
        return ScanSignal(
            kind="red_card.signal",
            summary="RED CARD (red_circle) from U1",
            payload={
                "row_id": row_id,
                "signal_kind": "red_circle",
                "user_id": "U1",
                "signal_text": ":red_circle:",
                "offending_message_text": "the offending message",
                "overlay": "acme",
            },
        )

    def test_creates_corrective_task_and_stamps_row_id(self) -> None:
        # On the pre-fix code red_card fell into _handle_orchestrator, whose
        # ``auto_start is not True`` guard returned None — nothing was created (RED).
        created = persist_agent_actions(_agent_actions(self._signal(row_id=42)))
        assert len(created) == 1
        task = created[0]
        assert task.phase == "coding"
        assert task.ticket.role == Ticket.Role.AUTHOR
        assert task.ticket.extra["red_card_signal_id"] == 42
        assert task.ticket.issue_url == "redcard://signal/42"

    def test_idempotent_across_ticks(self) -> None:
        first = persist_agent_actions(_agent_actions(self._signal(row_id=43)))
        second = persist_agent_actions(_agent_actions(self._signal(row_id=43)))
        assert len(first) == 1
        assert second == []


class TestE2eFixZoneRevived(TestCase):
    def _signal(self, *, spec: str = "e2e/specs/login.spec.ts") -> ScanSignal:
        return ScanSignal(
            kind="e2e.failure_detected",
            summary=f"Failed E2E: {spec}",
            payload={"spec": spec, "test_title": "login flow", "skill_overlay": "acme", "ts": "1.2"},
        )

    def test_creates_author_e2e_task(self) -> None:
        created = persist_agent_actions(_agent_actions(self._signal()))
        assert len(created) == 1
        assert created[0].phase == "e2e"
        assert created[0].ticket.role == Ticket.Role.AUTHOR

    def test_idempotent_across_ticks(self) -> None:
        persist_agent_actions(_agent_actions(self._signal(spec="e2e/specs/a.spec.ts")))
        second = persist_agent_actions(_agent_actions(self._signal(spec="e2e/specs/a.spec.ts")))
        assert second == []


class TestSkillDriftZoneRevived(TestCase):
    def _signal(self, *, repo: str = "/repos/skills", file_path: str = "code/SKILL.md") -> ScanSignal:
        return ScanSignal(
            kind="skill_drift_detected",
            summary=f"drift {file_path}",
            payload={"repo": repo, "file_path": file_path, "finding_fingerprint": "fp1", "overlay": "acme"},
        )

    def test_creates_author_coding_task(self) -> None:
        created = persist_agent_actions(_agent_actions(self._signal()))
        assert len(created) == 1
        assert created[0].phase == "coding"
        assert created[0].ticket.role == Ticket.Role.AUTHOR


class TestAnswererZoneRevived(TestCase):
    def _signal(self, *, event_id: int = 55) -> ScanSignal:
        return ScanSignal(
            kind="incoming_event.task_needed",
            summary="task request (answering): what is X?",
            payload={"event_id": event_id, "phase": "answering", "detail": "what is X?", "target_ref": ""},
        )

    def test_creates_author_answering_task(self) -> None:
        created = persist_agent_actions(_agent_actions(self._signal()))
        assert len(created) == 1
        assert created[0].phase == "answering"
        assert created[0].ticket.role == Ticket.Role.AUTHOR


class TestFailLoudOnUnhandledZone(TestCase):
    def test_unhandled_zone_records_persist_error(self) -> None:
        # An agent zone that is neither handled nor persisted-at-source is a
        # DROPPED dispatch — it must surface in errors (action_needed), not a
        # silent logger.debug (#1 blocker fail-loud contract).
        action = DispatchAction(kind="agent", zone="t3:never-registered", detail="?", payload={"url": "x"})
        errors: dict[str, str] = {}
        created = persist_agent_actions([action], errors=errors)
        assert created == []
        assert errors["persist:t3:never-registered"]

    def test_handled_zone_records_no_error(self) -> None:
        # Anti-vacuity: a genuinely-handled zone must NOT record a spurious error.
        signal = ScanSignal(kind="my_pr.failed", summary="x", payload={"pr_url": "https://x/pr/1", "head_sha": "s1"})
        errors: dict[str, str] = {}
        persist_agent_actions(_agent_actions(signal), errors=errors)
        assert errors == {}

    def test_pending_task_reemission_is_a_silent_no_op(self) -> None:
        # A pending_task re-emission (carrying task_id) of a persisted-at-source
        # zone is a deliberate no-op — no row, no error.
        action = DispatchAction(
            kind="agent", zone="t3:coder", detail="pending", payload={"task_id": 99, "phase": "coding"}
        )
        errors: dict[str, str] = {}
        created = persist_agent_actions([action], errors=errors)
        assert created == []
        assert errors == {}


class TestFullDispatchPersistWire(TestCase):
    def test_every_revived_zone_yields_exactly_one_task(self) -> None:
        """One end-to-end sweep: each revived zone's signal yields exactly one Task."""
        signals = [
            ScanSignal(
                kind="my_pr.failed", summary="x", payload={"pr_url": "https://w/pr/1", "head_sha": "w1", "overlay": "o"}
            ),
            ScanSignal(
                kind="codex_review.dispatch",
                summary="x",
                payload={
                    "slug": "o/r",
                    "pr_id": 200,
                    "head_sha": "w2",
                    "pr_url": "https://w/pr/2",
                    "variant": "codex:review",
                    "overlay": "o",
                },
            ),
            ScanSignal(
                kind="red_card.signal",
                summary="x",
                payload={"row_id": 300, "signal_kind": "red_circle", "overlay": "o"},
            ),
            ScanSignal(
                kind="e2e.failure_detected", summary="x", payload={"spec": "e2e/z.spec.ts", "skill_overlay": "o"}
            ),
            ScanSignal(
                kind="skill_drift_detected", summary="x", payload={"repo": "/r", "file_path": "f.md", "overlay": "o"}
            ),
            ScanSignal(
                kind="incoming_event.task_needed",
                summary="x",
                payload={"event_id": 400, "phase": "answering", "detail": "q"},
            ),
        ]
        actions = [a for s in signals for a in dispatch([s]) if a.kind == "agent"]
        created = persist_agent_actions(actions)
        assert len(created) == len(signals), f"expected one task per revived zone, got {[t.phase for t in created]}"

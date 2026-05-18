"""``DispatchGapDetector`` per-detector tests (BLUEPRINT § 5.7 / plan §8)."""

import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import SelfImproveFiring, Session, Task, Ticket
from teatree.loop.self_improve.actions import run_action_ladder
from teatree.loop.self_improve.detectors import DispatchGapDetector


class DispatchGapDetectorTests(TestCase):
    def _ticket(self, n: int) -> Ticket:
        return Ticket.objects.create(overlay="acme", issue_url=f"https://example.com/issues/{n}")

    def _pending_task(self, ticket: Ticket) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        return Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)

    def _empty_registry(self, tmp_path: Path) -> None:
        registry = tmp_path / "consolidation-registry.json"
        registry.write_text("{}", encoding="utf-8")

    def _holder_registry(self, tmp_path: Path, agent_id: str = "agent-a") -> None:
        registry = tmp_path / "consolidation-registry.json"
        registry.write_text(json.dumps({agent_id: {"session_id": "s1"}}), encoding="utf-8")

    def test_fires_when_smell_present(self) -> None:
        ticket = self._ticket(1)
        self._pending_task(ticket)
        with patch.dict("os.environ", {"T3_LOOP_REGISTRY_DIR": "/tmp/__nonexistent_registry__"}):
            reports = DispatchGapDetector().detect()
        assert len(reports) == 1
        assert reports[0].severity == "warn"
        assert "pending" in reports[0].summary

    def test_does_not_fire_when_smell_absent(self) -> None:
        # No pending tasks ⇒ no smell.
        with patch.dict("os.environ", {"T3_LOOP_REGISTRY_DIR": "/tmp/__nonexistent_registry__"}):
            assert DispatchGapDetector().detect() == []

    def test_does_not_fire_when_consolidation_holder_present(self, tmp_path: Path | None = None) -> None:
        ticket = self._ticket(2)
        self._pending_task(ticket)
        # When a holder is present in the registry, the smell is "someone
        # else is on it", so we hold off.
        from tempfile import TemporaryDirectory  # noqa: PLC0415

        with TemporaryDirectory() as td:
            tmp = Path(td)
            self._holder_registry(tmp)
            with patch.dict("os.environ", {"T3_LOOP_REGISTRY_DIR": str(tmp)}):
                reports = DispatchGapDetector().detect()
        assert reports == []

    def test_dedup_within_cooldown(self) -> None:
        ticket = self._ticket(3)
        self._pending_task(ticket)
        with patch.dict("os.environ", {"T3_LOOP_REGISTRY_DIR": "/tmp/__nonexistent_registry__"}):
            reports1 = DispatchGapDetector().detect()
            for r in reports1:
                run_action_ladder(r)
            reports2 = DispatchGapDetector().detect()
            for r in reports2:
                run_action_ladder(r)
        # Same dedup_key, same state_hash (one pending task) ⇒ one firing.
        assert SelfImproveFiring.objects.filter(detector="dispatch_gap").count() == 1
        assert SelfImproveFiring.objects.get(detector="dispatch_gap").action_count == 1

    def test_action_ladder_ceiling(self) -> None:
        """Ceiling is ``ticket`` per the issue plan."""
        ticket = self._ticket(4)
        self._pending_task(ticket)
        with patch.dict("os.environ", {"T3_LOOP_REGISTRY_DIR": "/tmp/__nonexistent_registry__"}):
            reports = DispatchGapDetector().detect()
        assert reports
        assert reports[0].max_rung == SelfImproveFiring.Action.TICKET.value

    def test_auto_fix_false(self) -> None:
        """Smell-only detector: never self-heals."""
        assert DispatchGapDetector.auto_fix is False

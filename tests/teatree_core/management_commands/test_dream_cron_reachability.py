"""Every dream promotion phase is reachable from the nightly cron ``tick`` (#4176).

``force_all_phases`` is the ``--full`` convenience alias for manual core-gap work.
The nightly tick runs eval derivation and live validation unconditionally. The
remaining optional memory-promotion phase is reachable through its own setting.

Each test drives ``dream tick`` and asserts that the phase's promoter runs.
The AST ratchet at the bottom refuses an AND-gate shape for future phases.
"""

import ast
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory, Loop
from teatree.core.models.ticket import Ticket
from teatree.hooks import _repo_visibility
from teatree.loops.dream.engine import DreamRunResult
from teatree.loops.dream.loop import DREAM_LOOP_NAME
from teatree.loops.dream.replay import ConsolidationExtract, WeightedSnippet
from tests._send_gate import allow_forge_repos

#: A memory-backed rule plus a correction turn violating it again.
_MEMORY_BODY = (
    "---\n"
    "name: feedback_askuserquestion_overuse\n"
    "metadata:\n  type: feedback\n"
    "---\n"
    "The AskUserQuestion gate must not fire for routine obstacles — make a reasonable guess and keep working.\n"
)
_VIOLATION_TURN = (
    '{"type": "user", "content": "I told you again — stop firing AskUserQuestion '
    'for routine obstacles, you do not follow instructions!!"}'
)

#: The remaining optional memory phase is disabled so tests isolate one route.
_MEMORY_PHASE_OFF = {
    "T3_DREAM_MEMORY_PROMOTE": "0",
}


@pytest.fixture(autouse=True)
def _configured_dream_publication(monkeypatch: pytest.MonkeyPatch, configured_banned_term_registry: None) -> None:
    """Cron dream writes use the classed term registry and allowed forge repo."""
    monkeypatch.setattr(_repo_visibility, "slug_visibility", lambda _slug: "PUBLIC")
    allow_forge_repos("souliane/teatree")


def _stateful_umbrella_host() -> CodeHostBackend:
    """An umbrella whose body persists across writes, so a re-read sees the last checkbox."""
    state = {"body": "## Open gaps\n"}

    def _update(**kwargs: object) -> dict[str, int]:
        state["body"] = str(kwargs["body"])
        return {"number": 2663}

    host = MagicMock(spec=CodeHostBackend)
    host.get_issue.side_effect = lambda *_a, **_k: {"body": state["body"]}
    host.update_issue.side_effect = _update
    return host


def _recurrence_result() -> DreamRunResult:
    extract = ConsolidationExtract(
        snippets=(
            WeightedSnippet(
                path=Path("/memory/feedback_askuserquestion_overuse.md"),
                kind="memory",
                weight=90,
                text=_MEMORY_BODY,
            ),
            WeightedSnippet(path=Path("/sessions/session-a.jsonl"), kind="main", weight=80, text=_VIOLATION_TURN),
        ),
    )
    return DreamRunResult(clusters_recorded=1, members_replayed=5, dry_run=False, snippets_distilled=2, extract=extract)


class DreamPhasesAreCronReachableTestCase(TestCase):
    """Every required phase runs from ``tick`` without ``--full``."""

    def setUp(self) -> None:
        super().setUp()
        Loop.objects.update_or_create(
            name=DREAM_LOOP_NAME,
            defaults={
                "script": "src/teatree/loops/dream/loop.py",
                "prompt": None,
                "delay_seconds": 86400,
                "daily_at": None,
                "enabled": True,
                "last_run_at": None,
            },
        )

    def _tick(self, host: object | None = None, **env: str) -> str:
        out = StringIO()
        with (
            patch("teatree.loops.dream.engine.run_consolidation", return_value=_recurrence_result()),
            patch("teatree.memory_audit.discover_memory_dirs", return_value=[]),
            patch(
                "teatree.core.management.commands.dream.Command._teatree_backlog_host",
                return_value=(host if host is not None else object(), "souliane/teatree"),
            ),
            patch.dict("os.environ", {**_MEMORY_PHASE_OFF, **env}, clear=False),
        ):
            call_command("dream", "tick", stdout=out)
        return out.getvalue()

    def test_compliance_escalation_runs_on_the_nightly_entry_point(self) -> None:
        with patch("teatree.loops.dream.compliance.run_compliance_escalation", return_value="") as escalate:
            self._tick()
        escalate.assert_called_once()

    def test_automation_asks_run_on_the_nightly_entry_point(self) -> None:
        with patch("teatree.loops.dream.automation_ask.run_automation_asks_phase", return_value="") as asks:
            self._tick()
        asks.assert_called_once()

    def test_live_validation_runs_from_tick_unconditionally(self) -> None:
        sentinel = object()
        seen: dict[str, object] = {}

        def _capture(_path: object, **kwargs: object) -> list:
            seen["validator"] = getattr(kwargs.get("live_gate"), "validator", "MISSING")
            return []

        with (
            patch("teatree.loops.dream.promote.build_live_validator", return_value=sentinel),
            patch("teatree.loops.dream.promote.promote_proposals_file", side_effect=_capture),
        ):
            self._tick()
        assert seen["validator"] is sentinel

    def test_memory_promotion_runs_from_tick_on_its_toggle_alone(self) -> None:
        with (
            patch("teatree.loops.dream.promote_memory.file_core_gap_tickets", return_value=[]) as promote,
            patch("teatree.loops.dream.batch_promote.reconcile_batches", return_value=[]),
        ):
            self._tick(T3_DREAM_MEMORY_PROMOTE="1")
        promote.assert_called_once()

    def test_eval_derivation_runs_from_tick_unconditionally(self) -> None:
        with (
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.loops.dream.llm_eval_proposer.stage_proposals_file", return_value=[]) as derive,
        ):
            self._tick()
        derive.assert_called_once()


class BatchedTickMintsNoTicketTestCase(DreamPhasesAreCronReachableTestCase):
    """A pass mints no ticket, whatever the gap count: its gaps queue for the backlog sweep (#4776)."""

    def test_many_pending_core_gaps_queue_on_the_umbrella_and_mint_nothing(self) -> None:
        umbrella = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/2663")
        for i in range(5):
            ConsolidatedMemory.objects.create(
                cluster_key=f"gap-{i}",
                rule=f"Run the tree-wide health gate before any push (gap-{i}).",
                source_files=[f"feedback_gap_{i}.md"],
                durable_destination="skills/ship/SKILL.md",
                member_count=1,
                max_member_weight=90,
                verified_citation="pushed without running the gate, CI went red",
            )
        with patch("teatree.loops.dream.compliance.run_compliance_escalation", return_value=""):
            self._tick(host=_stateful_umbrella_host(), T3_DREAM_MEMORY_PROMOTE="1")
        umbrella.refresh_from_db()
        assert not Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).exists()
        assert len(umbrella.extra["dream_gap_pending"]) == 5

    def test_zero_pending_gaps_queues_nothing(self) -> None:
        # The required negative control: a pass that promotes nothing queues nothing.
        umbrella = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/2663")
        with patch("teatree.loops.dream.compliance.run_compliance_escalation", return_value=""):
            self._tick(host=_stateful_umbrella_host(), T3_DREAM_MEMORY_PROMOTE="1")
        umbrella.refresh_from_db()
        assert "dream_gap_pending" not in umbrella.extra


class ForceAllPhasesIsNeverTheOnlyGateTestCase(TestCase):
    """Structural ratchet: no phase gate ANDs on a bare ``force_all_phases`` (#4176).

    The OR idiom ``if not force_all_phases and not <toggle>()`` is a ``UnaryOp`` operand,
    so it is NOT flagged — only a bare ``force_all_phases`` inside an ``and`` is, which is
    exactly the shape that makes a phase unreachable from the cron path.
    """

    def test_no_phase_gate_ands_on_force_all_phases(self) -> None:
        # Both files, because a gate that MOVES between them must stay covered — scanning
        # only the command would go vacuous the moment a phase migrates to the package.
        sources = [
            Path("src/teatree/core/management/commands/dream.py"),
            *Path("src/teatree/loops/dream").rglob("*.py"),
        ]
        offenders = {
            f"{source}:{node.lineno}"
            for source in sources
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"), filename=str(source)))
            if isinstance(node, ast.BoolOp)
            and isinstance(node.op, ast.And)
            and any(isinstance(v, ast.Name) and v.id == "force_all_phases" for v in node.values)
        }
        assert not offenders, (
            f"bare `force_all_phases` inside an `and` at {sorted(offenders)} — "
            "the cron tick never sets it, so that phase is unreachable from the nightly pass. "
            "Gate on the phase's own config toggle, or use `not force_all_phases and not <toggle>()`."
        )

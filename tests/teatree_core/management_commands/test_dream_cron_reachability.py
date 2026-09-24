"""Every dream promotion phase is reachable from the nightly cron ``tick`` (#4176).

``force_all_phases`` is the ``--full`` convenience alias — "turn every opt-in phase on
for this one manual pass". It must never be a phase's ONLY way in, because ``tick`` (the
cron entry) never sets it: a phase AND-gated on it is dead on the cron path however its
own toggle is set. Measured before the fix: 355 consecutive nightly passes produced zero
escalations and zero promotions.

Each test drives ``dream tick`` with ONLY one phase's config toggle on and asserts that
phase's promoter runs. The AST ratchet at the bottom refuses the AND-gate shape
structurally, so a NEW phase cannot reintroduce the class.
"""

import ast
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConsolidatedMemory, Loop
from teatree.core.models.ticket import Ticket
from teatree.loops.dream.engine import DreamRunResult
from teatree.loops.dream.loop import DREAM_LOOP_NAME
from teatree.loops.dream.replay import ConsolidationExtract, WeightedSnippet

#: A memory-backed rule plus a correction turn violating it again — a RECURRENCE, the
#: only finding kind the compliance-escalation phase acts on.
_MEMORY_BODY = (
    "name: feedback_askuserquestion_overuse\n"
    "The AskUserQuestion gate must not fire for routine obstacles — make a reasonable guess and keep working.\n"
)
_VIOLATION_TURN = (
    '{"type": "user", "content": "I told you again — stop firing AskUserQuestion '
    'for routine obstacles, you do not follow instructions!!"}'
)

#: Every phase toggle silenced, so a test enabling one toggle measures that phase alone.
_ALL_PHASES_OFF = {
    "T3_DREAM_PROPOSE_EVALS": "0",
    "T3_DREAM_CROSS_LINK": "0",
    "T3_DREAM_MERGE": "0",
    "T3_DREAM_REINDEX": "0",
    "T3_DREAM_DECAY": "0",
    "T3_DREAM_MEMORY_PROMOTE": "0",
    "T3_DREAM_DERIVE_EVALS": "0",
    "T3_DREAM_AUTOMATION_ASKS": "0",
    "T3_DREAM_COMPLIANCE_ESCALATE": "0",
    "T3_DREAM_VALIDATE_LIVE": "0",
}


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
    """Each promotion phase runs from ``tick`` on its own config toggle — no ``--full``."""

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
            patch.dict("os.environ", {**_ALL_PHASES_OFF, **env}, clear=False),
        ):
            call_command("dream", "tick", stdout=out)
        return out.getvalue()

    def test_compliance_escalation_runs_from_tick_on_its_toggle_alone(self) -> None:
        # RED before the fix: the gate was `force_all_phases and compliance_escalate_enabled()`,
        # and tick never sets force_all_phases — so the toggle was dead on the cron path.
        with patch("teatree.loops.dream.compliance.run_compliance_escalation", return_value="") as escalate:
            self._tick(T3_DREAM_COMPLIANCE_ESCALATE="1")
        escalate.assert_called_once()

    def test_live_validation_runs_from_tick_on_its_toggle_alone(self) -> None:
        # RED before the fix: validate_live had NO config path at all — only --full /
        # --validate-live — so every candidate the nightly pass cleared stayed withheld.
        sentinel = object()
        seen: dict[str, object] = {}

        def _capture(_path: object, **kwargs: object) -> list:
            seen["validator"] = getattr(kwargs.get("live_gate"), "validator", "MISSING")
            return []

        with (
            patch("teatree.loops.dream.promote.build_live_validator", return_value=sentinel),
            patch("teatree.loops.dream.promote.promote_proposals_file", side_effect=_capture),
        ):
            self._tick(T3_DREAM_PROPOSE_EVALS="1", T3_DREAM_VALIDATE_LIVE="1")
        assert seen["validator"] is sentinel

    def test_live_validation_stays_off_by_default_so_tick_still_withholds(self) -> None:
        seen: dict[str, object] = {}

        def _capture(_path: object, **kwargs: object) -> list:
            seen["validator"] = getattr(kwargs.get("live_gate"), "validator", "MISSING")
            return []

        with patch("teatree.loops.dream.promote.promote_proposals_file", side_effect=_capture):
            self._tick(T3_DREAM_PROPOSE_EVALS="1")
        assert seen["validator"] is None

    def test_memory_promotion_runs_from_tick_on_its_toggle_alone(self) -> None:
        with (
            patch("teatree.loops.dream.promote_memory.file_core_gap_tickets", return_value=[]) as promote,
            patch("teatree.loops.dream.umbrella_ledger.reconcile_merged_gaps", return_value=[]),
        ):
            self._tick(T3_DREAM_MEMORY_PROMOTE="1")
        promote.assert_called_once()

    def test_automation_asks_runs_from_tick_on_its_toggle_alone(self) -> None:
        with patch("teatree.loops.dream.automation_ask.run_automation_asks_phase", return_value="") as phase:
            self._tick(T3_DREAM_AUTOMATION_ASKS="1")
        phase.assert_called_once()

    def test_eval_derivation_runs_from_tick_on_its_toggle_alone(self) -> None:
        with (
            patch("teatree.loops.dream.promote.promote_proposals_file", return_value=[]),
            patch("teatree.loops.dream.llm_eval_proposer.stage_proposals_file", return_value=[]) as derive,
        ):
            self._tick(T3_DREAM_PROPOSE_EVALS="1", T3_DREAM_DERIVE_EVALS="1")
        derive.assert_called_once()


class BatchedTickMintsOneTicketTestCase(DreamPhasesAreCronReachableTestCase):
    """A pass's promotions collapse into ONE ticket, whatever the gap count (#4776).

    ``promotion_cap`` used to bound how much of a pass's backlog got a ticket without
    bounding the fan-out itself — a measured pass with 297 pending gaps and the
    cap-of-5 default deferred roughly 60 nights to drain. It is deleted: there is
    nothing left to ration, because every gap a pass promotes now collapses into ONE
    ticket, not one per gap.
    """

    def test_many_pending_core_gaps_schedule_exactly_one_ticket(self) -> None:
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
        self._tick(host=_stateful_umbrella_host(), T3_DREAM_MEMORY_PROMOTE="1")
        batch_tickets = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True)
        assert batch_tickets.count() == 1
        assert len(batch_tickets.first().extra["dream_gap_batch"]) == 5

    def test_zero_pending_gaps_mints_no_ticket(self) -> None:
        # The required negative control: a pass that promotes nothing creates nothing.
        self._tick(host=_stateful_umbrella_host(), T3_DREAM_MEMORY_PROMOTE="1")
        assert not Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).exists()


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

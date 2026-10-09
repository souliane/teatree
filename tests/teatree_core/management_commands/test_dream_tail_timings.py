"""Per-phase tail timing and the terminal-outcome stamp (#4671).

The deployed pass was SIGKILLed by its external deadline 1313s after the distiller
stopped, short of its gates and its marker. Which tail phase consumed that time was
unattributable, because the pass emitted no per-phase timing at all — so the tail is
timed here, and the pass records WHY it ended so a kill is never read as a refusal.
"""

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.management.commands._dream_report import TailTimings
from teatree.core.models import DreamRunMarker
from teatree.core.models.dream_run_marker import OUTCOME_GATES_FAILED


class TailTimingsTestCase(TestCase):
    def test_summary_names_every_phase_slowest_first(self) -> None:
        ticks = iter([0.0, 5.0, 5.0, 105.0, 105.0, 106.0])
        timings = TailTimings(clock=lambda: next(ticks))
        with timings.phase("eval-promote"):
            pass
        with timings.phase("compliance"):
            pass
        with timings.phase("memory-phases+gates"):
            pass
        summary = timings.summary
        assert summary.startswith("; tail 106s (")
        assert summary.index("compliance 100s") < summary.index("eval-promote 5s")
        assert "memory-phases+gates 1s" in summary

    def test_a_raising_phase_is_still_timed(self) -> None:
        ticks = iter([0.0, 7.0])
        timings = TailTimings(clock=lambda: next(ticks))
        boom = RuntimeError("a tail phase that raises must still report its cost")
        with pytest.raises(RuntimeError), timings.phase("compliance"):
            raise boom
        assert "compliance 7s" in timings.summary

    def test_no_phases_yields_no_clause(self) -> None:
        assert TailTimings().summary == ""


class TerminalOutcomeStampTestCase(TestCase):
    def test_a_gate_refusal_records_the_rendered_failure_for_the_doctor(self) -> None:
        now = timezone.now()
        DreamRunMarker.objects.mark_attempted(now)  # the pre-pass clear
        DreamRunMarker.objects.mark_attempted(
            now, outcome=OUTCOME_GATES_FAILED, failure_detail="gates FAILED in memory (interference FAIL (1 lost))"
        )
        row = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert row.last_outcome == OUTCOME_GATES_FAILED
        assert "interference FAIL" in row.last_failure_detail

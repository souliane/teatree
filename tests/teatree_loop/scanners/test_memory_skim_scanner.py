"""Behaviour tests for :class:`MemorySkimScanner`.

Directive 32 (a restatement of 6 and 2): skim the Claude memories each week and
promote anything that concerns teatree's behaviour out of personal memory and
into the repo, asking the owner what to promote and what to drop when unsure.
The classifier already existed as a manual command; nothing ran it on a cadence,
so the promotable set accumulated. This scanner is the weekly caller, and its
deliverable is the owner-facing promote/drop question.
"""

from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import DeferredQuestion
from teatree.loop.scanners.memory_skim import MemorySkimScanner, skim_question_text
from teatree.memory_audit import MemoryEntry


def _entry(name: str, skill: str = "rules") -> MemoryEntry:
    return MemoryEntry(
        path=Path(f"/tmp/{name}.md"),
        name=name,
        entry_type="guardrail",
        body="NEVER let the factory do X.",
        matched_patterns=(r"\bNEVER\b",),
        suggested_skill=skill,
    )


class TestMemorySkimScanner(TestCase):
    def _scan(self, entries: list[MemoryEntry]) -> list:
        with patch("teatree.memory_audit.scan_all", return_value=entries):
            return MemorySkimScanner().scan()

    def test_nothing_promotable_asks_nothing(self) -> None:
        assert self._scan([]) == []
        assert not DeferredQuestion.objects.exists()

    def test_promotable_entries_raise_one_promote_or_drop_question(self) -> None:
        signals = self._scan([_entry("never-force-push", "ship"), _entry("worktree-first", "workspace")])

        assert len(signals) == 1
        assert signals[0].kind == "memory.skim_promotable"
        assert signals[0].payload["promotable"] == 2

        question = DeferredQuestion.objects.get()
        assert "never-force-push" in question.question
        assert "worktree-first" in question.question
        assert "promote" in question.question.lower()
        assert "drop" in question.question.lower()

    def test_a_second_scan_in_the_same_week_never_re_asks(self) -> None:
        self._scan([_entry("never-force-push")])
        assert self._scan([_entry("never-force-push"), _entry("another-one")]) == []

        assert DeferredQuestion.objects.count() == 1

    def test_an_answered_question_is_not_re_asked_within_the_same_week(self) -> None:
        self._scan([_entry("never-force-push")])
        DeferredQuestion.objects.get().apply_answer("promote it", resolved_via=DeferredQuestion.ResolvedVia.SLACK)

        assert self._scan([_entry("never-force-push")]) == []
        assert DeferredQuestion.objects.count() == 1

    def test_the_week_marker_scopes_the_dedupe(self) -> None:
        self._scan([_entry("never-force-push")])

        marker = DeferredQuestion.objects.get().dedupe_marker

        assert marker.startswith("memory-skim:")
        assert "-W" in marker

    def test_the_question_caps_the_list_and_names_the_remainder(self) -> None:
        entries = [_entry(f"memory-{i}") for i in range(23)]

        text = skim_question_text(entries)

        assert "+3 more" in text
        assert "memory-22" not in text

    def test_an_unreadable_memory_tree_never_breaks_the_tick(self) -> None:
        with patch("teatree.memory_audit.scan_all", side_effect=OSError("no such dir")):
            assert MemorySkimScanner().scan() == []

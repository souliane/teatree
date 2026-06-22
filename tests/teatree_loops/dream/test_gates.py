"""§4 acceptance gates (a)-(f) for the dream consolidation pass (#2545, #1933 § 4).

The gates make a pass ANTI-VACUOUS: a do-nothing, delete-only, or over-compressing
consolidation must FAIL at least one gate, while a faithful pass PASSES all six.
Each gate is proven in both directions — a faithful snapshot PASSES, and a
degenerate one (lost answer, regressed pass-rate, no consolidation, blown index
budget, lost audit trail) FAILS — so a vacuous gate that always passed would be
caught.

The probe corpus is seeded from the memory set (one probe per file, keyed on a
signature line) and replayed against a before/after :class:`MemorySnapshot`. The
``DreamQaProbe`` persistence layer is exercised by ``TestPersistProbeResults``
so the model is no longer dead — a recorded run accumulates pass/run counts.

Fixture-only with explicit snapshots: no LLM, no real ``~/.claude``, no wall clock.
"""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from teatree.core.models import DreamQaProbe
from teatree.loops.dream import gates, reindex
from teatree.loops.dream.decay import ArchivedMemory
from teatree.loops.dream.gates import (
    DreamQaReport,
    Gate,
    MemorySnapshot,
    QaProbe,
    derive_probes,
    evaluate_gates,
    persist_probe_results,
    probe_answerable,
    run_acceptance_pass,
    snapshot_memory_dir,
)


def _snapshot(memories: dict[str, str], index: str = "") -> MemorySnapshot:
    return MemorySnapshot.build(memories=memories, index_text=index)


class TestSnapshot(SimpleTestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_snapshot_reads_memory_set_and_index(self) -> None:
        (self.dir / "mem_a.md").write_text("name: mem_a\nlesson A body\n", encoding="utf-8")
        (self.dir / "mem_b.md").write_text("name: mem_b\nlesson B body\n", encoding="utf-8")
        (self.dir / "MEMORY.md").write_text("# index\n- a\n- b\n", encoding="utf-8")

        snap = snapshot_memory_dir(self.dir)

        assert set(snap.memories) == {"mem_a.md", "mem_b.md"}
        assert "lesson A body" in snap.memories["mem_a.md"]
        assert "# index" in snap.index_text
        assert snap.byte_size > 0
        assert snap.index_line_count == 3

    def test_missing_dir_is_empty_snapshot(self) -> None:
        snap = snapshot_memory_dir(self.dir / "absent")
        assert snap.memories == {}
        assert snap.index_text == ""


class TestDeriveAndReplay(SimpleTestCase):
    def test_derive_one_probe_per_memory_keyed_on_signature(self) -> None:
        snap = _snapshot({"mem_a.md": "name: mem_a\nthe load-bearing lesson A\n"})
        probes = derive_probes(snap)
        assert len(probes) == 1
        assert probes[0].source_name == "mem_a.md"
        assert "load-bearing lesson A" in probes[0].expected_answer

    def test_probe_answerable_when_signature_present(self) -> None:
        probe = QaProbe(question="q", expected_answer="the load-bearing lesson A", source_name="mem_a.md")
        snap = _snapshot({"mem_a.md": "name: mem_a\nthe load-bearing lesson A is still here\n"})
        assert probe_answerable(probe, snap) is True

    def test_probe_unanswerable_when_signature_gone(self) -> None:
        probe = QaProbe(question="q", expected_answer="the load-bearing lesson A", source_name="mem_a.md")
        snap = _snapshot({"mem_b.md": "name: mem_b\nan unrelated body\n"})
        assert probe_answerable(probe, snap) is False

    def test_probe_answerable_from_the_index_too(self) -> None:
        # A lesson transferred into the index line still counts as answerable.
        probe = QaProbe(question="q", expected_answer="the load-bearing lesson A", source_name="mem_a.md")
        snap = _snapshot({}, index="- the load-bearing lesson A — see topic file")
        assert probe_answerable(probe, snap) is True


class TestGateA(SimpleTestCase):
    def test_passes_when_every_pre_answerable_probe_still_answerable(self) -> None:
        before = _snapshot({"m.md": "name: m\nfact ONE and fact TWO\n"})
        after = _snapshot({"m.md": "name: m\nfact ONE and fact TWO consolidated\n"})
        probes = [
            QaProbe(question="q1", expected_answer="fact ONE", source_name="m.md"),
            QaProbe(question="q2", expected_answer="fact TWO", source_name="m.md"),
        ]
        result = Gate.retention(probes, before, after)
        assert result.passed

    def test_fails_a_delete_only_pass_that_drops_an_answer(self) -> None:
        before = _snapshot({"m.md": "name: m\nfact ONE and fact TWO\n"})
        after = _snapshot({})  # delete-only: the memory (and its answers) are gone
        probes = [
            QaProbe(question="q1", expected_answer="fact ONE", source_name="m.md"),
            QaProbe(question="q2", expected_answer="fact TWO", source_name="m.md"),
        ]
        result = Gate.retention(probes, before, after)
        assert not result.passed
        assert result.regressions  # the lost probes are named


class TestGateB(SimpleTestCase):
    def test_passes_when_prior_session_score_does_not_regress(self) -> None:
        after = _snapshot({"m.md": "name: m\nprior fact still recalled\n"})
        prior = [QaProbe(question="q", expected_answer="prior fact still recalled", source_name="m.md")]
        result = Gate.interference(prior, after, prior_pass_rate=1.0)
        assert result.passed

    def test_fails_when_a_new_rule_corrupts_a_prior_answer(self) -> None:
        after = _snapshot({"m.md": "name: m\nthe answer was overwritten by a new cluster\n"})
        prior = [QaProbe(question="q", expected_answer="prior fact still recalled", source_name="m.md")]
        result = Gate.interference(prior, after, prior_pass_rate=1.0)
        assert not result.passed


class TestGateC(SimpleTestCase):
    def test_passes_when_net_size_reduced_and_pruned_lines_homed(self) -> None:
        before = _snapshot({"a.md": "x" * 1000, "b.md": "y" * 1000}, index="- a\n- b\n")
        after = _snapshot({"a.md": "x" * 200}, index="- a\n")
        # the pruned index line ("b") has a confirmed durable home
        result = Gate.consolidation_happened(before, after, schema_before=0, schema_after=2, homed_index_lines={"- b"})
        assert result.passed

    def test_fails_a_do_nothing_pass(self) -> None:
        same = _snapshot({"a.md": "x" * 1000}, index="- a\n")
        result = Gate.consolidation_happened(same, same, schema_before=2, schema_after=2, homed_index_lines=set())
        assert not result.passed

    def test_passes_when_schema_count_increased_even_if_size_grew(self) -> None:
        before = _snapshot({"a.md": "x" * 100}, index="- a\n")
        after = _snapshot({"a.md": "x" * 100, "b.md": "y" * 100}, index="- a\n- b\n")
        result = Gate.consolidation_happened(before, after, schema_before=0, schema_after=3, homed_index_lines=set())
        assert result.passed

    def test_fails_when_a_pruned_line_has_no_durable_home(self) -> None:
        before = _snapshot({"a.md": "x" * 1000}, index="- a\n- b\n")
        after = _snapshot({"a.md": "x" * 100}, index="- a\n")  # 'b' line vanished
        result = Gate.consolidation_happened(before, after, schema_before=0, schema_after=1, homed_index_lines=set())
        assert not result.passed  # pruned '- b' has no confirmed durable home

    def test_passes_when_clusters_recorded_even_if_files_grew(self) -> None:
        # A real distillation pass lands rules in the DB ledger; the on-disk file
        # set may grow (cross-link links appended) yet consolidation DID happen.
        before = _snapshot({"a.md": "x" * 100}, index="- a\n")
        after = _snapshot({"a.md": "x" * 150}, index="- a\n")  # grew (links appended)
        result = Gate.consolidation_happened(
            before, after, schema_before=0, schema_after=0, homed_index_lines=set(), clusters_recorded=2
        )
        assert result.passed

    def test_clusters_recorded_does_not_excuse_an_unhomed_prune(self) -> None:
        before = _snapshot({"a.md": "x" * 100}, index="- a\n- b\n")
        after = _snapshot({"a.md": "x" * 100}, index="- a\n")  # 'b' pruned, no home
        result = Gate.consolidation_happened(
            before, after, schema_before=0, schema_after=0, homed_index_lines=set(), clusters_recorded=2
        )
        assert not result.passed  # consolidation happened but a pruned line is orphaned

    def test_homes_a_reworded_pointer_to_a_surviving_memory(self) -> None:
        # Re-index (phase 5) clips a long curated summary to <=200 chars: the index
        # LINE text changes, but feedback_x.md still exists and is still pointed at —
        # the pointer was reworded, not lost. A reworded pointer is NOT a lost lesson,
        # so the consolidation gate must NOT flag it as an unhomed prune (#2545 defect:
        # this perpetually blocked the success marker, keeping staleness firing).
        long_line = "- [feedback_x.md](feedback_x.md) — BINDING: " + "do the best autonomously; " * 12
        clipped_line = "- [feedback_x.md](feedback_x.md) — BINDING: do the best autonomously"
        before = _snapshot({"feedback_x.md": "real body, no summary verbatim"}, index=long_line + "\n")
        after = _snapshot({"feedback_x.md": "real body, no summary verbatim"}, index=clipped_line + "\n")
        result = Gate.consolidation_happened(
            before, after, schema_before=0, schema_after=0, homed_index_lines=set(), clusters_recorded=3
        )
        assert result.passed

    def test_a_pruned_pointer_to_a_vanished_memory_is_still_unhomed(self) -> None:
        # The fix must NOT excuse a genuine loss: feedback_x.md is GONE from the set
        # and its lesson is not findable elsewhere -> the pruned pointer stays unhomed.
        line = "- [feedback_x.md](feedback_x.md) — a real lesson"
        before = _snapshot({"feedback_x.md": "the lesson body", "a.md": "x" * 100}, index=line + "\n- a\n")
        after = _snapshot({"a.md": "x" * 100}, index="- a\n")  # feedback_x.md archived/deleted
        result = Gate.consolidation_happened(
            before, after, schema_before=0, schema_after=0, homed_index_lines=set(), clusters_recorded=3
        )
        assert not result.passed

    def test_passes_on_zero_clusters_when_maintenance_was_performed(self) -> None:
        # A quiet-night pass: 0 NEW clusters distilled, no net size drop, no schema
        # growth — but the file-side phases cross-linked edges / re-indexed / decayed.
        # That IS real consolidation maintenance, so the gate must PASS (#2626 staleness).
        same = _snapshot({"a.md": "x" * 100}, index="- a\n")
        result = Gate.consolidation_happened(
            same,
            same,
            schema_before=2,
            schema_after=2,
            homed_index_lines=set(),
            clusters_recorded=0,
            maintenance_performed=True,
        )
        assert result.passed

    def test_true_no_op_still_fails_even_without_maintenance(self) -> None:
        # NOTHING happened: 0 clusters, no size drop, no schema growth, no maintenance.
        # The no-op detection must stay intact — the gate FAILS with the no-consolidation
        # detail rather than being weakened into always-pass.
        same = _snapshot({"a.md": "x" * 100}, index="- a\n")
        result = Gate.consolidation_happened(
            same,
            same,
            schema_before=2,
            schema_after=2,
            homed_index_lines=set(),
            clusters_recorded=0,
            maintenance_performed=False,
        )
        assert not result.passed
        assert "no consolidation" in result.detail

    def test_maintenance_does_not_excuse_an_unhomed_prune(self) -> None:
        before = _snapshot({"a.md": "x" * 100}, index="- a\n- b\n")
        after = _snapshot({"a.md": "x" * 100}, index="- a\n")  # 'b' pruned, no home
        result = Gate.consolidation_happened(
            before,
            after,
            schema_before=0,
            schema_after=0,
            homed_index_lines=set(),
            clusters_recorded=0,
            maintenance_performed=True,
        )
        assert not result.passed  # maintenance happened but a pruned line is orphaned

    def test_summary_name_dropping_a_live_memory_does_not_home_a_gone_target(self) -> None:
        # The pruned line's link TARGET (gone_x.md) vanished, but its free-text summary
        # mentions a DIFFERENT, surviving memory's filename. Homing keys on the link
        # target only, never a .md token in the summary — else a real loss is masked.
        line = "- [gone_x.md](gone_x.md) — superseded; see feedback_live.md for context"
        before = _snapshot(
            {"gone_x.md": "the lost lesson", "feedback_live.md": "x" * 50},
            index=line + "\n- [feedback_live.md](feedback_live.md) — live\n",
        )
        after = _snapshot({"feedback_live.md": "x" * 50}, index="- [feedback_live.md](feedback_live.md) — live\n")
        result = Gate.consolidation_happened(
            before, after, schema_before=0, schema_after=0, homed_index_lines=set(), clusters_recorded=3
        )
        assert not result.passed  # gone_x.md is gone; the summary's mention of a live file must not home it


class TestGateD(SimpleTestCase):
    def test_passes_under_budget(self) -> None:
        after = _snapshot({}, index="- one line\n- two line\n")
        result = Gate.index_budget(after)
        assert result.passed

    def test_fails_over_line_budget(self) -> None:
        big_index = "\n".join(f"- line {i}" for i in range(gates.INDEX_LINE_BUDGET + 5))
        after = _snapshot({}, index=big_index)
        result = Gate.index_budget(after)
        assert not result.passed

    def test_fails_over_byte_budget(self) -> None:
        after = _snapshot({}, index="- " + "x" * (gates.INDEX_BYTE_BUDGET + 10))
        result = Gate.index_budget(after)
        assert not result.passed


class TestGateE(SimpleTestCase):
    def test_passes_when_second_pass_rate_not_lower(self) -> None:
        assert Gate.monotonicity(pass_rate_first=0.8, pass_rate_second=0.8).passed
        assert Gate.monotonicity(pass_rate_first=0.8, pass_rate_second=0.9).passed

    def test_fails_when_second_pass_rate_lower(self) -> None:
        assert not Gate.monotonicity(pass_rate_first=0.9, pass_rate_second=0.7).passed


class TestGateF(SimpleTestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _archived(self, name: str, *, write: bool) -> ArchivedMemory:
        dest = self.dir / "archive" / f"{name}.md"
        if write:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("archived body", encoding="utf-8")
        return ArchivedMemory(
            name=name, source=self.dir / f"{name}.md", destination=dest, reason="stale, unreferenced, durably homed"
        )

    def test_passes_when_every_archived_entry_is_restorable(self) -> None:
        archived = [self._archived("mem_a", write=True), self._archived("mem_b", write=True)]
        result = Gate.no_loss_audit(archived)
        assert result.passed

    def test_fails_when_an_archived_entry_is_missing_its_destination(self) -> None:
        archived = [self._archived("mem_a", write=True), self._archived("mem_b", write=False)]  # b never landed
        result = Gate.no_loss_audit(archived)
        assert not result.passed

    def test_empty_archive_is_a_clean_pass(self) -> None:
        assert Gate.no_loss_audit([]).passed


class TestEvaluateGates(SimpleTestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_faithful_pass_passes_every_gate(self) -> None:
        before = _snapshot({"a.md": "name: a\nfact ONE\n", "b.md": "name: b\nfact TWO\n"}, index="- a\n- b\n")
        after = _snapshot({"a.md": "name: a\nfact ONE and fact TWO consolidated\n"}, index="- a\n")
        report = evaluate_gates(
            snapshot_before=before,
            snapshot_after=after,
            schema_before=0,
            schema_after=2,
            homed_index_lines={"- b"},
            prior_pass_rate=1.0,
            pass_rate_first=1.0,
            pass_rate_second=1.0,
            archived=[],
        )
        assert report.passed
        assert all(g.passed for g in report.gate_results)

    def test_delete_only_pass_fails_overall(self) -> None:
        before = _snapshot({"a.md": "name: a\nfact ONE\n"}, index="- a\n")
        after = _snapshot({}, index="")  # everything deleted, nothing homed
        report = evaluate_gates(
            snapshot_before=before,
            snapshot_after=after,
            schema_before=0,
            schema_after=0,
            homed_index_lines=set(),
            prior_pass_rate=1.0,
            pass_rate_first=1.0,
            pass_rate_second=0.0,
            archived=[],
        )
        assert not report.passed
        failed = {g.name for g in report.gate_results if not g.passed}
        assert "retention" in failed  # gate (a) catches the dropped answer


class TestPersistProbeResults(TestCase):
    """The DreamQaProbe model is now POPULATED — no longer a dead model (#2545)."""

    def test_records_one_result_per_probe_accumulating_counts(self) -> None:
        after = _snapshot({"m.md": "name: m\nfact ONE present\n"})
        probes = [
            QaProbe(question="q1", expected_answer="fact ONE", source_name="m.md"),
            QaProbe(question="q2", expected_answer="fact MISSING", source_name="m.md"),
        ]
        persist_probe_results(probes, after, overlay="acme")

        assert DreamQaProbe.objects.current_corpus("acme").count() == 2
        q1 = DreamQaProbe.objects.get(question="q1")
        q2 = DreamQaProbe.objects.get(question="q2")
        assert q1.pass_count == 1
        assert q2.pass_count == 0

    def test_idempotent_on_probe_key_accumulates_across_runs(self) -> None:
        after = _snapshot({"m.md": "name: m\nfact ONE present\n"})
        probes = [QaProbe(question="q1", expected_answer="fact ONE", source_name="m.md")]

        persist_probe_results(probes, after, overlay="acme")
        persist_probe_results(probes, after, overlay="acme")

        assert DreamQaProbe.objects.count() == 1
        row = DreamQaProbe.objects.get(question="q1")
        assert row.run_count == 2
        assert row.pass_count == 2

    def test_marks_prior_session_on_re_record(self) -> None:
        after = _snapshot({"m.md": "name: m\nfact ONE present\n"})
        probes = [QaProbe(question="q1", expected_answer="fact ONE", source_name="m.md")]
        persist_probe_results(probes, after, overlay="acme")
        persist_probe_results(probes, after, overlay="acme")
        assert DreamQaProbe.objects.prior_session_probes("acme").count() == 1


class TestRunAcceptancePass(TestCase):
    """The command's per-dir entry point: gates + DreamQaProbe population (#2545)."""

    def test_faithful_pass_passes_and_populates_the_corpus(self) -> None:
        before = _snapshot(
            {"a.md": "name: a\nfact ONE\n", "b.md": "name: b\nfact TWO\n"}, index="- fact ONE\n- fact TWO\n"
        )
        after = _snapshot({"a.md": "name: a\nfact ONE and fact TWO consolidated\n"}, index="- fact ONE\n- fact TWO\n")
        report = run_acceptance_pass(before, after, overlay="acme", archived=[], schema_before=0, schema_after=2)
        assert report.passed
        # The formerly-dead model is now populated.
        assert DreamQaProbe.objects.current_corpus("acme").count() == 2

    def test_delete_only_pass_fails_the_retention_gate(self) -> None:
        before = _snapshot({"a.md": "name: a\nfact ONE\n"}, index="- fact ONE\n")
        after = _snapshot({}, index="")  # delete-only
        report = run_acceptance_pass(before, after, overlay="acme", archived=[], schema_before=0, schema_after=0)
        assert not report.passed
        assert "retention" in {g.name for g in report.gate_results if not g.passed}

    def test_second_run_uses_the_recorded_prior_baseline(self) -> None:
        # First run records a 100% prior baseline (the prior corpus is keyed on the
        # 'a.md' question, expected_answer 'a recalled fact').
        snap = _snapshot({"a.md": "name: a\na recalled fact\n"}, index="- a recalled fact\n")
        run_acceptance_pass(snap, snap, overlay="acme", archived=[], schema_before=0, schema_after=1)
        # A second pass where the prior fact is now entirely gone regresses the
        # prior-session pass-rate -> interference (and retention) fail against the
        # recorded 100% baseline.
        regressed = _snapshot({"a.md": "name: a\nan unrelated replacement\n"}, index="- unrelated\n")
        report = run_acceptance_pass(snap, regressed, overlay="acme", archived=[], schema_before=1, schema_after=1)
        failed = {g.name for g in report.gate_results if not g.passed}
        assert "interference" in failed or "retention" in failed

    def test_dry_run_does_not_persist(self) -> None:
        snap = _snapshot({"a.md": "name: a\nfact ONE\n"}, index="- fact ONE\n")
        run_acceptance_pass(snap, snap, overlay="acme", archived=[], schema_before=0, schema_after=1, persist=False)
        assert DreamQaProbe.objects.count() == 0

    def test_reindex_clipping_a_long_curated_summary_still_passes(self) -> None:
        # End-to-end #2545 staleness defect: re-index (phase 5) clips a >200-char
        # curated MEMORY.md summary, the gate flagged the old long line as an unhomed
        # prune, and the success marker was never stamped (last_succeeded_at froze).
        # The pointer still targets the live memory file, so the pass must PASS.
        d = Path(tempfile.mkdtemp()) / "memory"
        d.mkdir()
        long_summary = "BINDING: " + "do the best autonomously and never introduce tech debt; " * 8
        (d / "feedback_x.md").write_text(
            f"---\nname: feedback_x\ndescription: {long_summary}\nmetadata:\n  type: feedback\n---\n\nbody\n",
            encoding="utf-8",
        )
        long_index = reindex.render_index(d).replace("feedback_x.md) — ", "feedback_x.md) — " + "EXTRA " * 40, 1)
        (d / "MEMORY.md").write_text(long_index, encoding="utf-8")
        before = snapshot_memory_dir(d)
        reindex.reindex_memory(d, dry_run=False)  # re-clips -> rewrites the long line
        after = snapshot_memory_dir(d)
        report = run_acceptance_pass(
            before, after, overlay="acme", archived=[], schema_before=0, schema_after=0, clusters_recorded=3
        )
        assert report.passed, [g.detail for g in report.gate_results if not g.passed]


class TestReportRender(SimpleTestCase):
    def test_render_names_each_failing_gate(self) -> None:
        report = DreamQaReport(
            gate_results=(
                gates.GateResult(name="retention", passed=False, detail="lost q1"),
                gates.GateResult(name="index_budget", passed=True, detail="ok"),
            )
        )
        rendered = report.render()
        assert "retention" in rendered
        assert "FAIL" in rendered

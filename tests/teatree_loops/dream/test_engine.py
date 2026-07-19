"""Tests for the dream distillation engine SEAM (#1933)."""

import json
import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream import distill, engine, sdk_distiller
from teatree.loops.dream.engine import (
    ConsolidationExtract,
    DistilledCluster,
    DistillEmptyReason,
    DistillResult,
    DreamRunResult,
    TranscriptMember,
    WeightedSnippet,
    WriteOutcome,
    build_extract,
    cluster_is_grounded,
    enumerate_members,
    normalize_ws,
    run_consolidation,
    write_clusters,
)
from teatree.loops.dream.eval_proposer import EvalProposalRequest, ProposedEval


class DreamRunResultTestCase(TestCase):
    def test_result_is_typed_and_frozen(self) -> None:
        result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=True)
        assert result.dry_run is True
        assert result.clusters_recorded == 0
        assert result.members_replayed == 0


def _no_clusters(_extract: ConsolidationExtract) -> list[DistilledCluster]:
    return []


class RunConsolidationSeamTestCase(TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_returns_dream_run_result(self) -> None:
        result = run_consolidation(overlay="", since=None, dry_run=False, distiller=_no_clusters)
        assert isinstance(result, DreamRunResult)

    def test_dry_run_writes_no_consolidated_memory_rows(self) -> None:
        run_consolidation(overlay="", since=None, dry_run=True, distiller=_no_clusters)
        assert ConsolidatedMemory.objects.count() == 0

    def test_dry_run_result_flags_dry_run(self) -> None:
        result = run_consolidation(overlay="", since=None, dry_run=True, distiller=_no_clusters)
        assert result.dry_run is True

    def test_no_clusters_distiller_writes_no_rows(self) -> None:
        run_consolidation(overlay="", since=None, dry_run=False, distiller=_no_clusters)
        assert ConsolidatedMemory.objects.count() == 0

    def test_truncated_extract_is_threaded_into_the_result_and_warned(self) -> None:
        # F6.7: ConsolidationExtract.truncated was computed and propagated but never
        # read. A pass that clipped/dropped high-signal drift for prompt budget now
        # surfaces it — threaded onto the result and logged at WARNING.
        big = "x" * 1_000_000
        members = [TranscriptMember(path=self.tmp / f"feedback_{i}.md", kind="memory") for i in range(50)]
        for member in members:
            member.path.write_text(big)
        with (
            patch.object(engine, "enumerate_members", return_value=members),
            self.assertLogs("teatree.loops.dream.engine", level="WARNING") as logs,
        ):
            result = run_consolidation(overlay="", since=None, dry_run=True, distiller=_no_clusters)
        assert result.extract_truncated is True
        assert any("TRUNCATED" in line for line in logs.output)

    def test_untruncated_extract_leaves_the_result_flag_false(self) -> None:
        members = [TranscriptMember(path=self.tmp / "feedback_a.md", kind="memory")]
        members[0].path.write_text("BINDING: short lesson")
        with patch.object(engine, "enumerate_members", return_value=members):
            result = run_consolidation(overlay="", since=None, dry_run=True, distiller=_no_clusters)
        assert result.extract_truncated is False

    def test_injected_distiller_receives_extract(self) -> None:
        seen: list[ConsolidationExtract] = []

        def _spy(extract: ConsolidationExtract) -> list[DistilledCluster]:
            seen.append(extract)
            return []

        with tempfile.TemporaryDirectory() as tmp:
            member = _write_member(Path(tmp))
            with patch.object(engine, "enumerate_members", return_value=[member]):
                run_consolidation(overlay="", since=None, dry_run=False, distiller=_spy)
        assert len(seen) == 1
        assert isinstance(seen[0], ConsolidationExtract)

    def test_result_carries_the_extract_and_distilled_count(self) -> None:
        # D1c: the result carries the bounded extract it built (so the command can
        # reuse it for the compliance/automatable-ask phases) and the honest
        # snippets_distilled count.
        with tempfile.TemporaryDirectory() as tmp:
            member = _write_member(Path(tmp))
            with patch.object(engine, "enumerate_members", return_value=[member]):
                result = run_consolidation(overlay="", since=None, dry_run=False, distiller=_no_clusters)
        assert result.extract is not None
        assert result.snippets_distilled == len(result.extract.snippets)
        assert result.snippets_distilled >= 1
        assert result.clusters_rejected == 0

    def test_result_counts_ungrounded_clusters_rejected(self) -> None:
        # D1c: an ungrounded distiller cluster is rejected by the ledger guard and the
        # count flows through to the result (surfaced in the command summary).
        def _ungrounded(extract: ConsolidationExtract) -> list[DistilledCluster]:
            snippet = extract.snippets[0]
            return [
                DistilledCluster(
                    cluster_key="x",
                    rule="an ungrounded rule",
                    source_files=[str(snippet.path)],
                    is_binding=False,
                    verified_citation="a quote absent from every cited snippet",
                    durable_destination="",
                )
            ]

        with tempfile.TemporaryDirectory() as tmp:
            member = _write_member(Path(tmp))
            with patch.object(engine, "enumerate_members", return_value=[member]):
                result = run_consolidation(overlay="", since=None, dry_run=False, distiller=_ungrounded)
        assert result.clusters_recorded == 0
        assert result.clusters_rejected == 1


_CITATION = "pushed without running the gate, CI went red"


def _cluster_for(member: TranscriptMember, *, key: str = "k1", binding: bool = False) -> DistilledCluster:
    return DistilledCluster(
        cluster_key=key,
        rule="Run the gate before pushing.",
        source_files=[str(member.path)],
        is_binding=binding,
        verified_citation=_CITATION,
        durable_destination="feedback/run_gate.md",
    )


def _write_member(
    tmp_path: Path,
    name: str = "feedback_x.md",
    body: str = f"BINDING: run the gate — {_CITATION}",
) -> TranscriptMember:
    f = tmp_path / name
    f.write_text(body)
    return TranscriptMember(path=f, kind="memory")


def _extract_of(*members: TranscriptMember) -> ConsolidationExtract:
    return build_extract(list(members))


class WriteClustersTestCase(TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_writes_one_row_per_valid_cluster(self) -> None:
        member = _write_member(self.tmp)
        outcome = write_clusters([_cluster_for(member)], _extract_of(member), dry_run=False)
        assert outcome == WriteOutcome(written=1, rejected=0)
        row = ConsolidatedMemory.objects.get(cluster_key="k1")
        assert row.rule == "Run the gate before pushing."
        assert row.source_files == [str(member.path)]
        assert row.is_binding is False

    def test_idempotent_rerun_writes_no_duplicate(self) -> None:
        member = _write_member(self.tmp)
        write_clusters([_cluster_for(member)], _extract_of(member), dry_run=False)
        write_clusters([_cluster_for(member)], _extract_of(member), dry_run=False)
        assert ConsolidatedMemory.objects.filter(cluster_key="k1").count() == 1

    def test_different_llm_slugs_same_members_upsert_to_one_row(self) -> None:
        # #2723: the distiller emits DIFFERENT slugs across two runs for the SAME
        # member set. With a DETERMINISTIC cluster_key (sha256 over the members) the
        # second run UPSERTS the existing row instead of creating a duplicate.
        member = _write_member(self.tmp)
        extract = _extract_of(member)
        payload_a = (
            f'[{{"cluster_key":"slug-run-1","rule":"Run the gate before pushing.",'
            f'"source_files":["{member.path}"],"is_binding":false,'
            f'"verified_citation":"{_CITATION}","durable_destination":"feedback/run_gate.md"}}]'
        )
        payload_b = payload_a.replace("slug-run-1", "a-completely-different-slug-run-2")
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload_a):
            clusters_a = sdk_distiller.sdk_distiller(extract)
        with patch.object(sdk_distiller, "_run_distiller_turn", return_value=payload_b):
            clusters_b = sdk_distiller.sdk_distiller(extract)

        write_clusters(clusters_a, extract, dry_run=False)
        write_clusters(clusters_b, extract, dry_run=False)
        # ONE row — the reworded slug did not fork a duplicate.
        assert ConsolidatedMemory.objects.count() == 1

    def test_rejects_cluster_with_empty_source_files(self) -> None:
        member = _write_member(self.tmp)
        bad = DistilledCluster(
            cluster_key="bad",
            rule="hallucinated",
            source_files=[],
            is_binding=False,
            verified_citation=_CITATION,
            durable_destination="",
        )
        outcome = write_clusters([bad], _extract_of(member), dry_run=False)
        assert outcome == WriteOutcome(written=0, rejected=1)
        assert not ConsolidatedMemory.objects.filter(cluster_key="bad").exists()

    def test_rejects_cluster_citing_unknown_path(self) -> None:
        member = _write_member(self.tmp)
        bad = DistilledCluster(
            cluster_key="bad",
            rule="hallucinated",
            source_files=["/nope/not-a-member.md"],
            is_binding=False,
            verified_citation=_CITATION,
            durable_destination="",
        )
        outcome = write_clusters([bad], _extract_of(member), dry_run=False)
        assert outcome == WriteOutcome(written=0, rejected=1)
        assert not ConsolidatedMemory.objects.filter(cluster_key="bad").exists()

    def test_rejects_cluster_with_blank_citation(self) -> None:
        member = _write_member(self.tmp)
        bad = DistilledCluster(
            cluster_key="bad",
            rule="rule",
            source_files=[str(member.path)],
            is_binding=False,
            verified_citation="   ",
            durable_destination="",
        )
        outcome = write_clusters([bad], _extract_of(member), dry_run=False)
        assert outcome == WriteOutcome(written=0, rejected=1)
        assert not ConsolidatedMemory.objects.filter(cluster_key="bad").exists()

    def test_rejects_real_path_with_invented_quote(self) -> None:
        member = _write_member(self.tmp)
        bad = DistilledCluster(
            cluster_key="bad",
            rule="rule",
            source_files=[str(member.path)],
            is_binding=False,
            verified_citation="a mistake that never appears in the snippet text",
            durable_destination="",
        )
        outcome = write_clusters([bad], _extract_of(member), dry_run=False)
        assert outcome == WriteOutcome(written=0, rejected=1)
        assert not ConsolidatedMemory.objects.filter(cluster_key="bad").exists()

    def test_accepts_citation_with_differing_whitespace(self) -> None:
        member = self._member_with_body("feedback_ws.md", f"line one\n  {_CITATION}\n  trailing")
        spaced = DistilledCluster(
            cluster_key="ws",
            rule="rule",
            source_files=[str(member.path)],
            is_binding=False,
            verified_citation=f"  {_CITATION}  ",
            durable_destination="",
        )
        outcome = write_clusters([spaced], _extract_of(member), dry_run=False)
        assert outcome == WriteOutcome(written=1, rejected=0)

    def test_dry_run_writes_nothing(self) -> None:
        member = _write_member(self.tmp)
        outcome = write_clusters([_cluster_for(member)], _extract_of(member), dry_run=True)
        assert outcome == WriteOutcome(written=1, rejected=0)
        assert ConsolidatedMemory.objects.count() == 0

    def test_write_outcome_counts_and_warns_on_rejected_cluster(self) -> None:
        # D1c: a grounded cluster is written; an ungrounded one is counted as rejected
        # AND logged at WARNING (silently dropped before), so an ungrounded distiller
        # batch is surfaced rather than swallowed.
        member = _write_member(self.tmp)
        good = _cluster_for(member)
        bad = DistilledCluster(
            cluster_key="bad",
            rule="an invented rule with no grounding",
            source_files=[str(member.path)],
            is_binding=False,
            verified_citation="a quote that never appears in the snippet text at all",
            durable_destination="",
        )
        with self.assertLogs("teatree.loops.dream.engine", level="WARNING") as logs:
            outcome = write_clusters([good, bad], _extract_of(member), dry_run=False)
        assert outcome == WriteOutcome(written=1, rejected=1)
        assert any("ungrounded cluster" in line for line in logs.output)

    def test_max_member_weight_is_the_cited_snippet_weight(self) -> None:
        member = _write_member(self.tmp)
        write_clusters([_cluster_for(member)], _extract_of(member), dry_run=False)
        row = ConsolidatedMemory.objects.get(cluster_key="k1")
        assert row.max_member_weight > 0

    def test_binding_row_rule_never_destructively_overwritten(self) -> None:
        member = _write_member(self.tmp)
        write_clusters([_cluster_for(member, binding=True)], _extract_of(member), dry_run=False)
        row = ConsolidatedMemory.objects.get(cluster_key="k1")
        assert row.is_binding is True
        original_rule = row.rule

        mutated = DistilledCluster(
            cluster_key="k1",
            rule="a totally different overwriting rule",
            source_files=[str(member.path)],
            is_binding=True,
            verified_citation=_CITATION,
            durable_destination="",
        )
        write_clusters([mutated], _extract_of(member), dry_run=False)
        row.refresh_from_db()
        assert row.rule == original_rule

    def test_persists_durable_destination_on_create(self) -> None:
        member = self._member_with_body("feedback_dd.md", f"line\n{_CITATION}\n")
        cluster = DistilledCluster(
            cluster_key="dd",
            rule="rule",
            source_files=[str(member.path)],
            is_binding=False,
            verified_citation=_CITATION,
            durable_destination="src/teatree/loops/dream/engine.py",
        )
        write_clusters([cluster], _extract_of(member), dry_run=False)
        row = ConsolidatedMemory.objects.get(cluster_key="dd")
        assert row.durable_destination == "src/teatree/loops/dream/engine.py"

    def test_rerun_updates_durable_destination_on_binding_row(self) -> None:
        member = self._member_with_body("feedback_ddb.md", f"line\n{_CITATION}\n")
        first = DistilledCluster(
            cluster_key="ddb",
            rule="rule",
            source_files=[str(member.path)],
            is_binding=True,
            verified_citation=_CITATION,
            durable_destination="",
        )
        write_clusters([first], _extract_of(member), dry_run=False)
        refreshed = DistilledCluster(
            cluster_key="ddb",
            rule="rule",
            source_files=[str(member.path)],
            is_binding=True,
            verified_citation=_CITATION,
            durable_destination="skills/code/SKILL.md",
        )
        write_clusters([refreshed], _extract_of(member), dry_run=False)
        row = ConsolidatedMemory.objects.get(cluster_key="ddb")
        assert row.is_binding is True
        assert row.durable_destination == "skills/code/SKILL.md"

    def test_core_destination_makes_triage_return_core_gap(self) -> None:
        from teatree.loops.dream.promote_memory import MemoryDisposition, triage_disposition  # noqa: PLC0415

        member = self._member_with_body("feedback_core.md", f"line\n{_CITATION}\n")
        cluster = DistilledCluster(
            cluster_key="core",
            rule="A generic teatree workflow gap.",
            source_files=[str(member.path)],
            is_binding=False,
            verified_citation=_CITATION,
            durable_destination="src/teatree/loops/dream/promote_memory.py",
        )
        write_clusters([cluster], _extract_of(member), dry_run=False)
        row = ConsolidatedMemory.objects.get(cluster_key="core")
        assert triage_disposition(row) is MemoryDisposition.CORE_GAP

    def test_rerun_refreshes_rule_for_non_binding(self) -> None:
        member = _write_member(self.tmp)
        write_clusters([_cluster_for(member)], _extract_of(member), dry_run=False)
        refreshed = DistilledCluster(
            cluster_key="k1",
            rule="A sharper restatement of the same lesson.",
            source_files=[str(member.path)],
            is_binding=False,
            verified_citation=_CITATION,
            durable_destination="",
        )
        write_clusters([refreshed], _extract_of(member), dry_run=False)
        row = ConsolidatedMemory.objects.get(cluster_key="k1")
        assert row.rule == "A sharper restatement of the same lesson."

    def test_rerun_updates_member_count_for_non_binding(self) -> None:
        member = _write_member(self.tmp)
        member2 = _write_member(self.tmp, name="feedback_y.md")
        first = DistilledCluster(
            cluster_key="k1",
            rule="rule",
            source_files=[str(member.path)],
            is_binding=False,
            verified_citation=_CITATION,
            durable_destination="",
        )
        write_clusters([first], _extract_of(member, member2), dry_run=False)
        grown = DistilledCluster(
            cluster_key="k1",
            rule="rule",
            source_files=[str(member.path), str(member2.path)],
            is_binding=False,
            verified_citation=_CITATION,
            durable_destination="",
        )
        write_clusters([grown], _extract_of(member, member2), dry_run=False)
        row = ConsolidatedMemory.objects.get(cluster_key="k1")
        assert row.member_count == 2
        assert sorted(row.source_files) == sorted([str(member.path), str(member2.path)])

    def _member_with_body(self, name: str, body: str) -> TranscriptMember:
        f = self.tmp / name
        f.write_text(body)
        return TranscriptMember(path=f, kind="memory")


class BuildExtractTestCase(TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _member(self, name: str, body: str, kind: str = "memory") -> TranscriptMember:
        f = self.tmp / name
        f.write_text(body)
        return TranscriptMember(path=f, kind=kind)

    def test_returns_consolidation_extract(self) -> None:
        member = self._member("feedback_a.md", "BINDING: do the thing")
        extract = build_extract([member])
        assert isinstance(extract, ConsolidationExtract)

    def test_binding_feedback_outranks_other(self) -> None:
        feedback = self._member("feedback_x.md", "BINDING: never push red")
        other = self._member("reference_y.md", "some neutral note")
        extract = build_extract([other, feedback])
        weights = {Path(s.path).name: s.weight for s in extract.snippets}
        assert weights["feedback_x.md"] > weights["reference_y.md"]

    def test_snippets_ranked_highest_weight_first(self) -> None:
        feedback = self._member("feedback_x.md", "BINDING: never push red")
        other = self._member("reference_y.md", "some neutral note")
        extract = build_extract([other, feedback])
        weights = [s.weight for s in extract.snippets]
        assert weights == sorted(weights, reverse=True)

    def test_raw_transcript_bulk_excluded_only_high_signal_lines(self) -> None:
        bulk = "\n".join(f'{{"type":"assistant","text":"chatter line {i}"}}' for i in range(200))
        signal = '{"type":"user","text":"TEATREE GATE BLOCK: pushed without running the gate"}'
        member = self._member("session.jsonl", bulk + "\n" + signal, kind="main")
        extract = build_extract([member])
        joined = "\n".join(s.text for s in extract.snippets)
        assert "TEATREE GATE BLOCK" in joined
        assert "chatter line 100" not in joined

    def test_size_is_bounded_and_truncated_flag_flips(self) -> None:
        big = "x" * 1_000_000
        members = [self._member(f"feedback_{i}.md", big) for i in range(50)]
        extract = build_extract(members)
        total = sum(len(s.text) for s in extract.snippets)
        assert total <= ConsolidationExtract.CHAR_CEILING
        assert extract.truncated is True

    def test_small_extract_is_not_truncated(self) -> None:
        member = self._member("feedback_a.md", "BINDING: short")
        extract = build_extract([member])
        assert extract.truncated is False

    def test_keeps_user_correction_prose_with_no_signal_keyword(self) -> None:
        chatter = "\n".join(f'{{"type":"assistant","text":"chatter {i}"}}' for i in range(50))
        correction = '{"type":"user","text":"I told you again — do not build a new banner, stop"}'
        member = self._member("session.jsonl", chatter + "\n" + correction, kind="main")
        extract = build_extract([member])
        joined = "\n".join(s.text for s in extract.snippets)
        assert "told you again" in joined
        assert "chatter 25" not in joined

    def test_keeps_repeated_near_identical_user_turn(self) -> None:
        repeated = "the config portal authoring UI is still missing from the deliverable"
        lines = [f'{{"type":"user","text":"{repeated}"}}' for _ in range(3)]
        lines.append('{"type":"assistant","text":"some neutral response with no cue"}')
        member = self._member("session.jsonl", "\n".join(lines), kind="main")
        extract = build_extract([member])
        joined = "\n".join(s.text for s in extract.snippets)
        assert repeated in joined

    def test_neutral_transcript_chatter_is_still_excluded(self) -> None:
        neutral = "\n".join(f'{{"type":"assistant","text":"computed result row {i}"}}' for i in range(40))
        member = self._member("session.jsonl", neutral, kind="main")
        extract = build_extract([member])
        joined = "\n".join(s.text for s in extract.snippets)
        assert "computed result row" not in joined

    def test_keeps_substantive_learning_prose_with_no_signal_keyword(self) -> None:
        # #2986: the day's richest drift is a declarative finding carrying no literal
        # signal token and no correction/ask cue. The keyword gate starved it before,
        # so a plain pass distilled 0 clusters from a corpus full of real learnings.
        chatter = "\n".join(f'{{"type":"assistant","text":"computed result row {i}"}}' for i in range(50))
        learning = '{"type":"assistant","text":"root caused the empty owner crash to a missing tenant filter"}'
        member = self._member("session.jsonl", chatter + "\n" + learning, kind="main")
        extract = build_extract([member])
        joined = "\n".join(s.text for s in extract.snippets)
        assert "root caused the empty owner crash" in joined
        assert "computed result row 25" not in joined

    def test_transcript_floor_survives_high_weight_memory_flood(self) -> None:
        flood = [self._member(f"feedback_{i}.md", "BINDING: " + ("x" * 50_000)) for i in range(20)]
        correction = '{"type":"user","text":"why did you do this again? do not, stop, I told you"}'
        transcript = self._member("session.jsonl", correction, kind="main")
        extract = build_extract([*flood, transcript])
        transcript_paths = {str(s.path) for s in extract.snippets if s.kind != "memory"}
        assert str(transcript.path) in transcript_paths

    def test_floor_keeps_multiple_transcripts_under_memory_flood(self) -> None:
        flood = [self._member(f"feedback_{i}.md", "BINDING: " + ("x" * 40_000)) for i in range(20)]
        transcripts = [
            self._member(
                f"session_{i}.jsonl",
                '{"type":"user","text":"stop — do not do that again, I told you not to"}',
                kind="main",
            )
            for i in range(5)
        ]
        extract = build_extract([*flood, *transcripts])
        kept_transcripts = {str(s.path) for s in extract.snippets if s.kind != "memory"}
        assert len(kept_transcripts) == 5

    def test_memory_floor_survives_task_output_flood(self) -> None:
        # RED before D1a/D1b: a task_output that merely QUOTES "BINDING" scored the
        # full BINDING floor (100) and — with no memory floor — flooded the whole
        # ceiling, starving the curated memory out of the prompt entirely (the pass
        # then distilled 0 real clusters). The memory floor + kind-aware weighting
        # guarantee the durable doctrine survives a night of large task outputs.
        memory = self._member("feedback_rule.md", "a durable feedback lesson about pushing the gate")
        flood = [self._member(f"task_{i}.output", "BINDING: " + ("x" * 8_000), kind="task_output") for i in range(20)]
        extract = build_extract([*flood, memory])
        kept_memories = {str(s.path) for s in extract.snippets if s.kind == "memory"}
        assert str(memory.path) in kept_memories

    def test_binding_quoting_transcript_does_not_outrank_memory(self) -> None:
        # RED before D1a: a transcript that only QUOTES a BINDING rule scored the full
        # BINDING floor (100) and OUTRANKED the feedback memory (90) that actually owns
        # the rule. Kind-aware weighting reserves the BINDING/feedback floors for curated
        # memory, so a quoting transcript can never tie or outrank the memory.
        memory = self._member("feedback_rule.md", "a durable feedback lesson", kind="memory")
        transcript = self._member("task_quote.output", "BINDING: never push to a red branch", kind="task_output")
        extract = build_extract([transcript, memory])
        weights = {s.kind: s.weight for s in extract.snippets}
        assert weights["memory"] >= weights["task_output"]


class CorrectionProseProducesGroundedClusterTestCase(TestCase):
    """A transcript carrying only correction prose still reaches the distiller and grounds (#1933)."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_correction_only_transcript_yields_a_grounded_cluster(self) -> None:
        body = '{"type":"user","text":"I told you again — do not build a new banner, stop"}'
        member = TranscriptMember(path=self.tmp / "session.jsonl", kind="main")
        member.path.write_text(body)

        def _distill(extract: ConsolidationExtract) -> list[DistilledCluster]:
            snippet = extract.snippets[0]
            return [
                DistilledCluster(
                    cluster_key="correction",
                    rule="Do not rebuild what the user told you not to.",
                    source_files=[str(snippet.path)],
                    is_binding=True,
                    verified_citation="do not build a new banner",
                    durable_destination="",
                )
            ]

        with patch.object(engine, "enumerate_members", return_value=[member]):
            run_consolidation(overlay="", since=None, dry_run=False, distiller=_distill)

        assert ConsolidatedMemory.objects.filter(cluster_key="correction").count() == 1

    def test_substantive_learning_only_transcript_yields_a_grounded_cluster(self) -> None:
        # #2986: a transcript whose only substance is a declarative learning (no
        # signal token, no correction/ask cue) must still reach the distiller and
        # ground a cluster. RED before the fix: the keyword gate dropped the line, the
        # extract was empty, the distiller was never called, 0 rows were written.
        chatter = "\n".join(f'{{"type":"assistant","text":"computed result row {i}"}}' for i in range(30))
        finding = '{"type":"assistant","text":"root caused the empty owner crash to a missing tenant filter"}'
        member = TranscriptMember(path=self.tmp / "session.jsonl", kind="main")
        member.path.write_text(chatter + "\n" + finding)

        def _distill(extract: ConsolidationExtract) -> list[DistilledCluster]:
            snippet = extract.snippets[0]
            return [
                DistilledCluster(
                    cluster_key="learning",
                    rule="Guard resolve_owner against a missing tenant filter.",
                    source_files=[str(snippet.path)],
                    is_binding=False,
                    verified_citation="root caused the empty owner crash to a missing tenant filter",
                    durable_destination="",
                )
            ]

        with patch.object(engine, "enumerate_members", return_value=[member]):
            run_consolidation(overlay="", since=None, dry_run=False, distiller=_distill)

        assert ConsolidatedMemory.objects.filter(cluster_key="learning").count() == 1


class EscapedJsonlCitationGroundsTestCase(TestCase):
    r"""A citation of a real .jsonl turn grounds once transcript content is decoded (#1933).

    A raw session ``.jsonl`` line JSON-escapes its content — an em-dash is ``\u2014``,
    an inner quote is ``\"``, a newline is ``\n``. The distiller reads the DECODED
    human text and quotes it verbatim, so before the extract shared that one decoded
    form the decoded citation was never a substring of the escaped snippet and every
    transcript-cited cluster was rejected as ungrounded. These tests pin the decoded
    representation AND prove the anti-hallucination substring gate still has teeth.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    @staticmethod
    def _realistic_session_jsonl() -> str:
        """A byte-for-byte realistic session transcript: content is JSON-escaped."""
        return "\n".join(
            json.dumps(obj)
            for obj in (
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "noise"}]}},
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": 'stop — do not build a new "banner" again\nI told you not to',
                    },
                },
            )
        )

    def _member(self) -> TranscriptMember:
        path = self.tmp / "session.jsonl"
        path.write_text(self._realistic_session_jsonl())
        return TranscriptMember(path=path, kind="main")

    def test_fixture_is_genuinely_escaped_on_disk(self) -> None:
        # Guards the test itself: if the fixture stopped escaping, the bug it
        # reproduces would silently vanish and the grounding assertion would be vacuous.
        on_disk = (self._member()).path.read_text()
        assert "\\u2014" in on_disk
        assert '\\"banner\\"' in on_disk

    def test_snippet_text_is_decoded_not_escaped(self) -> None:
        extract = build_extract([self._member()])
        joined = "\n".join(s.text for s in extract.snippets)
        assert "—" in joined
        assert '"banner"' in joined
        assert "\\u2014" not in joined
        assert '\\"' not in joined

    def test_decoded_citation_grounds(self) -> None:
        # RED before the fix: the decoded quote is not a substring of the escaped
        # snippet, so the ledger rejected the cluster and recorded nothing.
        member = self._member()

        def _distill(extract: ConsolidationExtract) -> list[DistilledCluster]:
            snippet = extract.snippets[0]
            return [
                DistilledCluster(
                    cluster_key="grounded",
                    rule="Do not rebuild what the user told you not to.",
                    source_files=[str(snippet.path)],
                    is_binding=True,
                    verified_citation='do not build a new "banner" again I told you not to',
                    durable_destination="",
                )
            ]

        with patch.object(engine, "enumerate_members", return_value=[member]):
            run_consolidation(overlay="", since=None, dry_run=False, distiller=_distill)

        assert ConsolidatedMemory.objects.filter(cluster_key="grounded").count() == 1

    def test_hallucinated_citation_is_still_rejected(self) -> None:
        # The gate MUST keep its teeth: an invented quote that never appears in the
        # decoded transcript is rejected, not recorded.
        member = self._member()

        def _distill(extract: ConsolidationExtract) -> list[DistilledCluster]:
            snippet = extract.snippets[0]
            return [
                DistilledCluster(
                    cluster_key="hallucinated",
                    rule="A rule the transcript never supports.",
                    source_files=[str(snippet.path)],
                    is_binding=False,
                    verified_citation="the user demanded a full rewrite of the auth layer",
                    durable_destination="",
                )
            ]

        with patch.object(engine, "enumerate_members", return_value=[member]):
            result = run_consolidation(overlay="", since=None, dry_run=False, distiller=_distill)

        assert result.clusters_recorded == 0
        assert result.clusters_rejected == 1
        assert ConsolidatedMemory.objects.filter(cluster_key="hallucinated").count() == 0


class GroundingPunctuationFoldTestCase(TestCase):
    """The grounding substring test folds smart/straight punctuation symmetrically (#1933).

    A decoded transcript may carry a straight quote where the model's citation used a
    curly one (or the reverse). Folding both operands to one canonical form keeps a
    genuine citation grounded while remaining a strict substring test — an invented
    quote is still rejected.
    """

    _SNIPPET: ClassVar[dict[str, str]] = {
        "/s.jsonl": normalize_ws('{"role": "user"} do not ship the "beta" build tonight')
    }

    def _cluster(self, citation: str) -> DistilledCluster:
        return DistilledCluster(
            cluster_key="k",
            rule="r",
            source_files=["/s.jsonl"],
            is_binding=False,
            verified_citation=citation,
            durable_destination="",
        )

    def test_smart_quote_citation_grounds_against_straight_quote_snippet(self) -> None:
        assert cluster_is_grounded(self._cluster("do not ship the “beta” build"), self._SNIPPET)

    def test_em_dash_citation_grounds_against_hyphen_snippet(self) -> None:
        snippet = {"/s.jsonl": normalize_ws('{"role": "user"} stop - do not build a new banner')}
        assert cluster_is_grounded(self._cluster("stop — do not build a new banner"), snippet)

    def test_invented_citation_still_rejected_after_fold(self) -> None:
        assert not cluster_is_grounded(self._cluster("ship the “release” build now"), self._SNIPPET)


class WeightedSnippetTestCase(TestCase):
    def test_is_frozen(self) -> None:
        snip = WeightedSnippet(path=Path("/x.md"), kind="memory", weight=5, text="t")
        with pytest.raises(AttributeError):
            snip.weight = 9  # type: ignore[misc]


class DistilledClusterTestCase(TestCase):
    def test_is_frozen(self) -> None:
        cluster = DistilledCluster(
            cluster_key="k",
            rule="r",
            source_files=["/x.md"],
            is_binding=False,
            verified_citation="c",
            durable_destination="",
        )
        with pytest.raises(AttributeError):
            cluster.rule = "other"  # type: ignore[misc]


class TestEnumerateMembersMainTranscripts:
    def test_recent_main_jsonl_is_included(self, tmp_path: Path) -> None:
        slug_dir = tmp_path / "slug"
        slug_dir.mkdir()
        jsonl = slug_dir / "session-abc.jsonl"
        jsonl.write_text('{"type":"user"}\n')

        members = enumerate_members(
            projects_dir=tmp_path,
            task_output_roots=[],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        assert any(m.path == jsonl and m.kind == "main" for m in members)

    def test_old_main_jsonl_is_excluded(self, tmp_path: Path) -> None:
        slug_dir = tmp_path / "slug"
        slug_dir.mkdir()
        jsonl = slug_dir / "old-session.jsonl"
        jsonl.write_text('{"type":"user"}\n')
        old_ts = time.time() - 3 * 24 * 3600
        os.utime(jsonl, (old_ts, old_ts))

        members = enumerate_members(
            projects_dir=tmp_path,
            task_output_roots=[],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        assert not any(m.path == jsonl for m in members)

    def test_no_members_when_projects_dir_empty(self, tmp_path: Path) -> None:
        members = enumerate_members(
            projects_dir=tmp_path,
            task_output_roots=[],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        assert members == []

    def test_file_reaped_after_recency_check_does_not_crash_the_sort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A /tmp session .jsonl is actively reaped: it can vanish between the recency
        # check and the mtime sort. The sort keys on the mtime captured up front, so a
        # now-deleted path never re-stats — the whole pass no longer crashes.
        slug = tmp_path / "slug"
        slug.mkdir()
        (slug / "a.jsonl").write_text("{}\n")
        (slug / "b.jsonl").write_text("{}\n")

        real_recent_mtime = engine._recent_file_mtime

        def reaping_probe(path: Path, cutoff_ts: float) -> float | None:
            mtime = real_recent_mtime(path, cutoff_ts)
            if mtime is not None:
                path.unlink()  # reaped right after the recency check, before the sort
            return mtime

        monkeypatch.setattr(engine, "_recent_file_mtime", reaping_probe)

        members = enumerate_members(
            projects_dir=tmp_path,
            task_output_roots=[],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        assert {m.path.name for m in members} == {"a.jsonl", "b.jsonl"}

    def test_nonexistent_projects_dir_returns_empty(self, tmp_path: Path) -> None:
        members = enumerate_members(
            projects_dir=tmp_path / "nonexistent",
            task_output_roots=[],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        assert members == []


class TestEnumerateMembersSubagentTranscripts:
    def test_subagent_jsonl_picked_up_as_subagent_kind(self, tmp_path: Path) -> None:
        subagent_dir = tmp_path / "slug" / "session-abc" / "subagents"
        subagent_dir.mkdir(parents=True)
        jsonl = subagent_dir / "agent-xyz.jsonl"
        jsonl.write_text('{"isSidechain":true}\n')

        members = enumerate_members(
            projects_dir=tmp_path,
            task_output_roots=[],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        assert any(m.path == jsonl and m.kind == "subagent" for m in members)

    def test_multiple_subagent_files_all_included(self, tmp_path: Path) -> None:
        subagent_dir = tmp_path / "slug" / "sess" / "subagents"
        subagent_dir.mkdir(parents=True)
        for i in range(3):
            (subagent_dir / f"agent-{i}.jsonl").write_text("{}\n")

        members = enumerate_members(
            projects_dir=tmp_path,
            task_output_roots=[],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        subagent_members = [m for m in members if m.kind == "subagent"]
        assert len(subagent_members) == 3


class TestEnumerateMembersTaskOutput:
    def test_task_output_file_picked_up_as_task_output_kind(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "slug" / "session-abc" / "tasks"
        tasks_dir.mkdir(parents=True)
        output_file = tasks_dir / "agent-id-123.output"
        output_file.write_text('{"isSidechain":true,"agentId":"agent-id-123"}\n')

        members = enumerate_members(
            projects_dir=tmp_path / "nonexistent",
            task_output_roots=[tmp_path],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        assert any(m.path == output_file and m.kind == "task_output" for m in members)

    def test_old_task_output_excluded(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "slug" / "session-abc" / "tasks"
        tasks_dir.mkdir(parents=True)
        output_file = tasks_dir / "old-agent.output"
        output_file.write_text("{}\n")
        old_ts = time.time() - 3 * 24 * 3600
        os.utime(output_file, (old_ts, old_ts))

        members = enumerate_members(
            projects_dir=tmp_path / "nonexistent",
            task_output_roots=[tmp_path],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        assert not any(m.path == output_file for m in members)

    def test_all_four_source_types_collected(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        task_root = tmp_path / "tasks_tmp"

        (projects / "slug").mkdir(parents=True)
        (projects / "slug" / "main-session.jsonl").write_text("{}\n")

        memory_dir = projects / "slug" / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "feedback_x.md").write_text("BINDING: a lesson\n")

        subagent_dir = projects / "slug" / "sess" / "subagents"
        subagent_dir.mkdir(parents=True)
        (subagent_dir / "agent-1.jsonl").write_text("{}\n")

        tasks_dir = task_root / "slug" / "sess" / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "agent-2.output").write_text("{}\n")

        members = enumerate_members(
            projects_dir=projects,
            task_output_roots=[task_root],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        kinds = {m.kind for m in members}
        assert kinds == {"memory", "main", "subagent", "task_output"}


class TestEnumerateMembersMemoryFiles:
    def test_memory_md_picked_up_as_memory_kind(self, tmp_path: Path) -> None:
        memory_dir = tmp_path / "slug" / "memory"
        memory_dir.mkdir(parents=True)
        md = memory_dir / "feedback_run_gate.md"
        md.write_text("BINDING: always run the gate\n")

        members = enumerate_members(
            projects_dir=tmp_path,
            task_output_roots=[],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        assert any(m.path == md and m.kind == "memory" for m in members)

    def test_old_memory_md_is_still_included(self, tmp_path: Path) -> None:
        # Curated memory files are durable — re-read regardless of age, unlike
        # the recency-gated transcripts.
        memory_dir = tmp_path / "slug" / "memory"
        memory_dir.mkdir(parents=True)
        md = memory_dir / "feedback_old.md"
        md.write_text("BINDING: an old lesson\n")
        old_ts = time.time() - 90 * 24 * 3600
        os.utime(md, (old_ts, old_ts))

        members = enumerate_members(
            projects_dir=tmp_path,
            task_output_roots=[],
            since=datetime.now(tz=UTC) - timedelta(hours=1),
        )

        assert any(m.path == md and m.kind == "memory" for m in members)


class TestTranscriptMember:
    def test_is_frozen(self, tmp_path: Path) -> None:
        member = TranscriptMember(path=tmp_path / "x.jsonl", kind="main")
        with pytest.raises(AttributeError):
            member.kind = "other"  # type: ignore[misc]


def _many_members(tmp_path: Path, count: int) -> list[TranscriptMember]:
    members: list[TranscriptMember] = []
    for i in range(count):
        f = tmp_path / f"feedback_{i:04d}.md"
        f.write_text(f"BINDING: lesson {i} — {_CITATION}")
        members.append(TranscriptMember(path=f, kind="memory"))
    return members


class ChunkedDistillTestCase(TestCase):
    """A large member set is distilled in batches and merged by cluster_key (#1933)."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_large_member_set_is_split_into_more_than_one_batch(self) -> None:
        members = _many_members(self.tmp, 9)
        extract = build_extract(members)
        seen_batch_sizes: list[int] = []

        def _spy(batch: ConsolidationExtract) -> list[DistilledCluster]:
            seen_batch_sizes.append(len(batch.snippets))
            return []

        with patch.dict(os.environ, {"T3_DREAM_MAX_DISTILL_MEMBERS": "4"}):
            distill.distill_in_batches(extract, distiller=_spy)

        assert len(seen_batch_sizes) > 1
        assert max(seen_batch_sizes) <= 4
        assert sum(seen_batch_sizes) == len(extract.snippets)

    def test_clusters_with_same_key_across_batches_are_merged_not_duplicated(self) -> None:
        members = _many_members(self.tmp, 8)
        extract = build_extract(members)

        def _spy(batch: ConsolidationExtract) -> list[DistilledCluster]:
            return [_cluster_for(TranscriptMember(path=batch.snippets[0].path, kind="memory"), key="shared")]

        with patch.dict(os.environ, {"T3_DREAM_MAX_DISTILL_MEMBERS": "3"}):
            outcome = distill.distill_in_batches(extract, distiller=_spy)

        keys = [c.cluster_key for c in outcome.clusters]
        assert keys.count("shared") == 1

    def test_run_consolidation_splits_oversized_extract(self) -> None:
        members = _many_members(self.tmp, 10)
        batch_count = {"n": 0}

        def _spy(batch: ConsolidationExtract) -> list[DistilledCluster]:
            batch_count["n"] += 1
            return []

        with (
            patch.object(engine, "enumerate_members", return_value=members),
            patch.dict(os.environ, {"T3_DREAM_MAX_DISTILL_MEMBERS": "3"}),
        ):
            run_consolidation(overlay="", since=None, dry_run=True, distiller=_spy)

        assert batch_count["n"] > 1

    def test_one_failing_batch_is_isolated_and_counted_not_fatal(self) -> None:
        # F6.4: a batch whose distiller call RAISES must not discard the clusters
        # already distilled from the other batches (paid LLM work). The failure is
        # isolated per batch, counted, and the surviving clusters still land.
        members = _many_members(self.tmp, 9)
        extract = build_extract(members)
        calls = {"n": 0}

        def _spy(batch: ConsolidationExtract) -> list[DistilledCluster]:
            calls["n"] += 1
            if calls["n"] == 2:  # the middle batch blows up
                msg = "distiller boom"
                raise RuntimeError(msg)
            return [_cluster_for(TranscriptMember(path=batch.snippets[0].path, kind="memory"), key=f"k{calls['n']}")]

        with patch.dict(os.environ, {"T3_DREAM_MAX_DISTILL_MEMBERS": "3"}):
            outcome = distill.distill_in_batches(extract, distiller=_spy)

        assert calls["n"] == 3  # all three batches were attempted, the failure did not abort
        assert outcome.failed_batches == 1
        assert {c.cluster_key for c in outcome.clusters} == {"k1", "k3"}  # the survivors landed


class SilentEmptyBatchTestCase(TestCase):
    """A batch returning 0 clusters from non-empty input is surfaced, not swallowed (#1933)."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_empty_from_nonempty_batch_is_counted(self) -> None:
        members = _many_members(self.tmp, 4)
        with patch.object(engine, "enumerate_members", return_value=members):
            result = run_consolidation(overlay="", since=None, dry_run=True, distiller=_no_clusters)
        assert result.empty_batches >= 1

    def test_empty_from_nonempty_batch_logs_warning(self) -> None:
        members = _many_members(self.tmp, 4)
        with (
            patch.object(engine, "enumerate_members", return_value=members),
            self.assertLogs("teatree.loops.dream.distill", level="WARNING") as captured,
        ):
            run_consolidation(overlay="", since=None, dry_run=True, distiller=_no_clusters)
        assert any("0 cluster" in line for line in captured.output)

    def test_productive_batch_does_not_flag_empty(self) -> None:
        members = _many_members(self.tmp, 4)

        def _one(batch: ConsolidationExtract) -> list[DistilledCluster]:
            return [_cluster_for(TranscriptMember(path=batch.snippets[0].path, kind="memory"))]

        with patch.object(engine, "enumerate_members", return_value=members):
            result = run_consolidation(overlay="", since=None, dry_run=True, distiller=_one)
        assert result.empty_batches == 0

    def test_empty_extract_does_not_flag_empty_batch(self) -> None:
        with patch.object(engine, "enumerate_members", return_value=[]):
            result = run_consolidation(overlay="", since=None, dry_run=True, distiller=_no_clusters)
        assert result.empty_batches == 0

    def test_empty_batch_warning_surfaces_a_broken_reason(self) -> None:
        # #2847: a distiller signalling an unparsable reply makes the 0-cluster WARNING
        # name WHY, so an operator can tell a broken parse from a healthy no-consolidation.
        members = _many_members(self.tmp, 2)
        extract = build_extract(members)

        def _broken(_batch: ConsolidationExtract) -> DistillResult:
            return DistillResult(clusters=[], empty_reason=DistillEmptyReason.UNPARSABLE)

        with self.assertLogs("teatree.loops.dream.distill", level="WARNING") as captured:
            distill.distill_in_batches(extract, distiller=_broken)
        assert any("unparsable" in line for line in captured.output)

    def test_empty_batch_warning_surfaces_a_healthy_reason(self) -> None:
        members = _many_members(self.tmp, 2)
        extract = build_extract(members)

        def _healthy(_batch: ConsolidationExtract) -> DistillResult:
            return DistillResult(clusters=[], empty_reason=DistillEmptyReason.NOTHING_TO_CONSOLIDATE)

        with self.assertLogs("teatree.loops.dream.distill", level="WARNING") as captured:
            distill.distill_in_batches(extract, distiller=_healthy)
        assert any("nothing_to_consolidate" in line for line in captured.output)


class RunConsolidationEvalProposalTestCase(TestCase):
    """``run_consolidation`` wires the default-off eval-candidate phase (#2346)."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_off_by_default_proposes_nothing_and_writes_no_queue(self) -> None:
        out = self.tmp / "queue.jsonl"
        result = run_consolidation(overlay="", since=None, dry_run=False, distiller=_no_clusters)
        assert result.evals_proposed == 0
        assert not out.exists()

    def test_request_writes_candidates_to_path(self) -> None:
        out = self.tmp / "queue.jsonl"
        sentinel = ProposedEval("x_under_load", "rule", _CITATION, ["f"], "")
        result = run_consolidation(
            overlay="",
            since=None,
            dry_run=False,
            distiller=_no_clusters,
            eval_proposals=EvalProposalRequest(proposer=lambda _c, _e: [sentinel], out_path=out),
        )
        assert result.evals_proposed == 1
        assert out.exists()
        assert len(out.read_text(encoding="utf-8").splitlines()) == 1

"""Tests for the dream distillation engine SEAM (#1933)."""

import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream import distill, engine
from teatree.loops.dream.engine import (
    ConsolidationExtract,
    DistilledCluster,
    DreamRunResult,
    TranscriptMember,
    WeightedSnippet,
    build_extract,
    enumerate_members,
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
        written = write_clusters([_cluster_for(member)], _extract_of(member), dry_run=False)
        assert written == 1
        row = ConsolidatedMemory.objects.get(cluster_key="k1")
        assert row.rule == "Run the gate before pushing."
        assert row.source_files == [str(member.path)]
        assert row.is_binding is False

    def test_idempotent_rerun_writes_no_duplicate(self) -> None:
        member = _write_member(self.tmp)
        write_clusters([_cluster_for(member)], _extract_of(member), dry_run=False)
        write_clusters([_cluster_for(member)], _extract_of(member), dry_run=False)
        assert ConsolidatedMemory.objects.filter(cluster_key="k1").count() == 1

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
        written = write_clusters([bad], _extract_of(member), dry_run=False)
        assert written == 0
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
        written = write_clusters([bad], _extract_of(member), dry_run=False)
        assert written == 0
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
        written = write_clusters([bad], _extract_of(member), dry_run=False)
        assert written == 0
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
        written = write_clusters([bad], _extract_of(member), dry_run=False)
        assert written == 0
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
        written = write_clusters([spaced], _extract_of(member), dry_run=False)
        assert written == 1

    def test_dry_run_writes_nothing(self) -> None:
        member = _write_member(self.tmp)
        written = write_clusters([_cluster_for(member)], _extract_of(member), dry_run=True)
        assert written == 1
        assert ConsolidatedMemory.objects.count() == 0

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


def _extract_with_one_snippet() -> ConsolidationExtract:
    return ConsolidationExtract(
        snippets=(WeightedSnippet(path=Path("/feedback_x.md"), kind="memory", weight=9, text="BINDING: x"),),
        truncated=False,
    )


class SdkDistillerParseTestCase(TestCase):
    def test_parses_clusters_from_json(self) -> None:
        payload = (
            '[{"cluster_key":"k1","rule":"do x","source_files":["/feedback_x.md"],'
            '"is_binding":true,"verified_citation":"the mistake","durable_destination":"d.md"}]'
        )
        with patch.object(engine, "_run_distiller_turn", return_value=payload):
            clusters = engine._sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1
        assert clusters[0].cluster_key == "k1"
        assert clusters[0].is_binding is True
        assert clusters[0].source_files == ["/feedback_x.md"]

    def test_parses_json_embedded_in_prose(self) -> None:
        payload = (
            "Here is the result:\n"
            '[{"cluster_key":"k1","rule":"do x","source_files":["/f.md"],'
            '"is_binding":false,"verified_citation":"m","durable_destination":""}]\n'
            "Done."
        )
        with patch.object(engine, "_run_distiller_turn", return_value=payload):
            clusters = engine._sdk_distiller(_extract_with_one_snippet())
        assert len(clusters) == 1

    def test_malformed_json_yields_no_clusters(self) -> None:
        with patch.object(engine, "_run_distiller_turn", return_value="not json at all"):
            clusters = engine._sdk_distiller(_extract_with_one_snippet())
        assert clusters == []

    def test_skips_entries_missing_required_keys(self) -> None:
        payload = (
            '[{"rule":"no key here"},'
            '{"cluster_key":"ok","rule":"r","source_files":["/f.md"],'
            '"is_binding":false,"verified_citation":"m","durable_destination":""}]'
        )
        with patch.object(engine, "_run_distiller_turn", return_value=payload):
            clusters = engine._sdk_distiller(_extract_with_one_snippet())
        assert [c.cluster_key for c in clusters] == ["ok"]

    def test_sdk_turn_failure_raises(self) -> None:
        with (
            patch.object(engine, "_run_distiller_turn", side_effect=RuntimeError("sdk boom")),
            pytest.raises(RuntimeError),
        ):
            engine._sdk_distiller(_extract_with_one_snippet())

    def test_empty_extract_short_circuits_without_sdk_call(self) -> None:
        empty = ConsolidationExtract(snippets=(), truncated=False)
        with patch.object(engine, "_run_distiller_turn") as turn:
            clusters = engine._sdk_distiller(empty)
        turn.assert_not_called()
        assert clusters == []


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

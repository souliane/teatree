"""Tests for the dream distillation engine SEAM (#1933)."""

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import ConsolidatedMemory
from teatree.loops.dream.engine import DreamRunResult, TranscriptMember, enumerate_members, run_consolidation


class DreamRunResultTestCase(TestCase):
    def test_result_is_typed_and_frozen(self) -> None:
        result = DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=True)
        assert result.dry_run is True
        assert result.clusters_recorded == 0
        assert result.members_replayed == 0


class RunConsolidationSeamTestCase(TestCase):
    def test_returns_dream_run_result(self) -> None:
        result = run_consolidation(overlay="", since=None, dry_run=False)
        assert isinstance(result, DreamRunResult)

    def test_dry_run_writes_no_consolidated_memory_rows(self) -> None:
        run_consolidation(overlay="", since=None, dry_run=True)
        assert ConsolidatedMemory.objects.count() == 0

    def test_dry_run_result_flags_dry_run(self) -> None:
        result = run_consolidation(overlay="", since=None, dry_run=True)
        assert result.dry_run is True

    def test_seam_writes_no_rows_until_engine_lands(self) -> None:
        run_consolidation(overlay="", since=None, dry_run=False)
        assert ConsolidatedMemory.objects.count() == 0


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

    def test_all_three_source_types_collected(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        task_root = tmp_path / "tasks_tmp"

        (projects / "slug").mkdir(parents=True)
        (projects / "slug" / "main-session.jsonl").write_text("{}\n")

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
        assert kinds == {"main", "subagent", "task_output"}


class TestTranscriptMember:
    def test_is_frozen(self, tmp_path: Path) -> None:
        member = TranscriptMember(path=tmp_path / "x.jsonl", kind="main")
        with pytest.raises(AttributeError):
            member.kind = "other"  # type: ignore[misc]

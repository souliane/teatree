"""File-side memory-phase runner for the dream cron (#2723).

The runner drives phases 4 / 4b / 5 / 6 and the §4 acceptance gates over the
discovered memory dirs, fault-isolated. These tests inject a TMP memory dir via
``discover_memory_dirs`` (the real ``~/.claude`` is never touched) and a fake
backlog-host resolver, exercising the fault-isolation and toggle paths directly.
"""

import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.loops.dream.phase_runner import MemoryPhaseRunner


def _no_host() -> tuple[None, str]:
    return None, "souliane/teatree"


class MemoryPhaseRunnerTestCase(TestCase):
    def setUp(self) -> None:
        self.memdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        topic = "the worktree provision lease pid claim guard owner liveness anchored"
        (self.memdir / "mem_a.md").write_text(f"name: mem_a\n{topic}\n", encoding="utf-8")
        (self.memdir / "mem_b.md").write_text(f"name: mem_b\n{topic} session\n", encoding="utf-8")
        self.runner = MemoryPhaseRunner(backlog_host_resolver=_no_host)

    def _patch_dirs(self) -> AbstractContextManager[object]:
        return patch("teatree.memory_audit.discover_memory_dirs", return_value=[self.memdir])

    def test_no_memory_dirs_is_a_clean_noop(self) -> None:
        with patch("teatree.memory_audit.discover_memory_dirs", return_value=[]):
            assert self.runner.run_memory_phases(dry_run=False) == ""
            summary, passed, gate_summary = self.runner.run_memory_phases_and_gates(clusters_recorded=0, dry_run=False)
        assert (summary, passed, gate_summary) == ("", True, "")

    def test_quiet_night_path_runs_all_phases(self) -> None:
        with self._patch_dirs(), patch.dict("os.environ", {}, clear=False):
            for env in ("T3_DREAM_CROSS_LINK", "T3_DREAM_MERGE", "T3_DREAM_REINDEX", "T3_DREAM_DECAY"):
                __import__("os").environ.pop(env, None)
            out = self.runner.run_memory_phases(dry_run=False)
        assert "cross-linked" in out
        assert "re-indexed" in out

    def test_decay_failure_is_warned_not_fatal(self) -> None:
        with (
            self._patch_dirs(),
            patch("teatree.loops.dream.decay.decay_memories", side_effect=RuntimeError("decay boom")),
        ):
            summary, _passed, _gate_summary = self.runner.run_memory_phases_and_gates(
                clusters_recorded=1, dry_run=False
            )
        # The phase failure is warned in the summary, not raised — the run continues.
        assert "WARN decay raised: RuntimeError" in summary

    def test_gate_evaluation_failure_is_warned_and_defaults_pass(self) -> None:
        with (
            self._patch_dirs(),
            patch(
                "teatree.loops.dream.gates.run_acceptance_pass",
                side_effect=RuntimeError("gate boom"),
            ),
        ):
            _summary, passed, gate_summary = self.runner.run_memory_phases_and_gates(clusters_recorded=1, dry_run=False)
        assert "WARN gates raised: RuntimeError" in gate_summary
        # A gate-machinery failure defaults that dir's verdict to PASS.
        assert passed is True

    def test_decay_toggle_off_archives_nothing(self) -> None:
        import os  # noqa: PLC0415
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        old = (datetime.now(tz=UTC) - timedelta(days=90)).timestamp()
        stale = self.memdir / "mem_old.md"
        stale.write_text("name: mem_old\nan old unreferenced lesson\n", encoding="utf-8")
        os.utime(stale, (old, old))
        with self._patch_dirs(), patch.dict("os.environ", {"T3_DREAM_DECAY": "0"}):
            out = self.runner.run_memory_phases(dry_run=False)
        assert "archived" not in out
        assert stale.exists()

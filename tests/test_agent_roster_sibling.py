# test-path: cross-cutting — a hooks/scripts sibling extraction; no src/teatree/ mirror.
"""The agent-roster sibling: one identity, re-export reachability, cold import.

The #778 sub-agent-dispatch capture was extracted whole out of ``hook_router``
into ``hooks/scripts/agent_roster.py`` (the #2384 router-split pattern) so the
shrink-only dispatcher nets smaller under #4107's two new handlers. These pin the
EXTRACTION contract (one identity, re-export reachability, cold import) plus the
capture's own edges — the tasks-dir fallback and the shapes that record nothing.
``test_pre_compact_snapshot_enriched.py`` covers the roster end to end through
the router re-export, unchanged by the move.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Never

import pytest

import hooks.scripts.agent_roster as roster
import hooks.scripts.hook_router as router

_SCRIPTS_DIR = Path(router.__file__).resolve().parent
_UNREADABLE = "tasks dir unreadable"


def _raising_glob(_self: Path, _pattern: str) -> Never:
    raise OSError(_UNREADABLE)


class TestCanonicalIdentity:
    def test_both_identities_are_one_module_object(self) -> None:
        assert sys.modules["hooks.scripts.agent_roster"] is roster
        assert sys.modules["agent_roster"] is roster


class TestRouterReExportReachable:
    def test_reexport_is_the_same_object(self) -> None:
        assert router.handle_track_agents is roster.handle_track_agents

    def test_it_is_still_registered_on_posttooluse(self) -> None:
        assert roster.handle_track_agents in router._HANDLERS["PostToolUse"]

    def test_patching_a_sibling_helper_is_seen_through_the_router(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        monkeypatch.setattr(roster, "_newest_task_agent_id", lambda: "a-from-the-sibling")
        router.handle_track_agents({"session_id": "sib", "tool_name": "Agent", "tool_input": {"description": "r"}})
        assert "a-from-the-sibling" in (tmp_path / "sib.agents").read_text(encoding="utf-8")


class TestTasksDirFallback:
    """The ``<agentId>.output`` scan used only when the payload omits the id."""

    def test_an_unreadable_tasks_dir_yields_no_id(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CLAUDE_TASKS_DIR", str(tmp_path / "nope"))
        monkeypatch.setattr(Path, "glob", _raising_glob)
        assert roster._newest_task_agent_id() == ""

    def test_an_empty_tasks_dir_yields_no_id(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CLAUDE_TASKS_DIR", str(tmp_path))
        assert roster._newest_task_agent_id() == ""

    def test_the_newest_output_file_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CLAUDE_TASKS_DIR", str(tmp_path))
        (tmp_path / "a-old.output").write_text("", encoding="utf-8")
        newest = tmp_path / "a-new.output"
        newest.write_text("", encoding="utf-8")
        os.utime(tmp_path / "a-old.output", (1, 1))
        assert roster._newest_task_agent_id() == "a-new"


class TestNoIdNoRecord:
    def test_a_non_agent_tool_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        roster.handle_track_agents({"session_id": "s", "tool_name": "Bash", "tool_input": {}})
        assert not (tmp_path / "s.agents").exists()

    def test_a_missing_session_id_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        roster.handle_track_agents({"tool_name": "Agent", "tool_input": {}})
        assert not (tmp_path / "s.agents").exists()

    def test_an_unresolvable_agent_id_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        monkeypatch.setattr(roster, "_newest_task_agent_id", lambda: "")
        roster.handle_track_agents({"session_id": "s", "tool_name": "Agent", "tool_input": {}})
        assert not (tmp_path / "s.agents").exists()

    def test_a_repeat_dispatch_of_one_id_appends_once(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        event = {"session_id": "s", "tool_name": "Agent", "tool_response": {"agentId": "a-1"}, "tool_input": {}}
        roster.handle_track_agents(event)
        roster.handle_track_agents(event)
        assert (tmp_path / "s.agents").read_text(encoding="utf-8").count("a-1") == 1

    def test_a_non_dict_response_carries_no_id(self) -> None:
        assert roster._agent_id_from_response("not-a-dict") == ""
        assert roster._agent_id_from_response({"agentId": "   "}) == ""


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """A fresh interpreter imports the sibling without Django configured or teatree loaded."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import agent_roster as r; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(r._agent_id_from_response({'agentId': 'a-1'}))"
                ),
                str(_SCRIPTS_DIR),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "a-1"

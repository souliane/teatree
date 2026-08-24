# test-path: cross-cutting — a hooks/scripts sibling extraction; no src/teatree/ mirror.
"""The dispatch-ledger sibling: one identity, re-export reachability, cold import.

The #778 sub-agent-dispatch capture was extracted whole out of ``hook_router``
into ``hooks/scripts/dispatch_ledger.py`` (the #2384 router-split pattern) so the
shrink-only dispatcher nets smaller under #4107's two new handlers. These pin the
EXTRACTION contract (one identity, re-export reachability, cold import) plus the
capture's own edges — the tasks-dir fallback and the shapes that record nothing.
``test_pre_compact_snapshot_enriched.py`` covers the ledger end to end through
the router re-export, unchanged by the move.

:class:`TestPostToolUseWireRegistration` is the #4131 rename's own guard: a hook
that cannot import fails OPEN, so a half-renamed module or alias would disable the
capture silently while every in-process assertion above still passed against the
directly-imported module. Only a real ``hook_router.py --event PostToolUse``
subprocess proves the registration survived.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Never

import pytest

import hooks.scripts.dispatch_ledger as ledger
import hooks.scripts.hook_router as router

_SCRIPTS_DIR = Path(router.__file__).resolve().parent
_HOOK_ROUTER = _SCRIPTS_DIR / "hook_router.py"
_UNREADABLE = "tasks dir unreadable"


def _raising_glob(_self: Path, _pattern: str) -> Never:
    raise OSError(_UNREADABLE)


class TestCanonicalIdentity:
    def test_both_identities_are_one_module_object(self) -> None:
        assert sys.modules["hooks.scripts.dispatch_ledger"] is ledger
        assert sys.modules["dispatch_ledger"] is ledger


class TestRouterReExportReachable:
    def test_reexport_is_the_same_object(self) -> None:
        assert router.handle_track_agents is ledger.handle_track_agents

    def test_it_is_still_registered_on_posttooluse(self) -> None:
        assert ledger.handle_track_agents in router._HANDLERS["PostToolUse"]

    def test_patching_a_sibling_helper_is_seen_through_the_router(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        monkeypatch.setattr(ledger, "_newest_task_agent_id", lambda: "a-from-the-sibling")
        router.handle_track_agents({"session_id": "sib", "tool_name": "Agent", "tool_input": {"description": "r"}})
        assert "a-from-the-sibling" in (tmp_path / "sib.agents").read_text(encoding="utf-8")


class TestTasksDirFallback:
    """The ``<agentId>.output`` scan used only when the payload omits the id."""

    def test_an_unreadable_tasks_dir_yields_no_id(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CLAUDE_TASKS_DIR", str(tmp_path / "nope"))
        monkeypatch.setattr(Path, "glob", _raising_glob)
        assert ledger._newest_task_agent_id() == ""

    def test_an_empty_tasks_dir_yields_no_id(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CLAUDE_TASKS_DIR", str(tmp_path))
        assert ledger._newest_task_agent_id() == ""

    def test_the_newest_output_file_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CLAUDE_TASKS_DIR", str(tmp_path))
        (tmp_path / "a-old.output").write_text("", encoding="utf-8")
        newest = tmp_path / "a-new.output"
        newest.write_text("", encoding="utf-8")
        os.utime(tmp_path / "a-old.output", (1, 1))
        assert ledger._newest_task_agent_id() == "a-new"


class TestNoIdNoRecord:
    def test_a_non_agent_tool_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        ledger.handle_track_agents({"session_id": "s", "tool_name": "Bash", "tool_input": {}})
        assert not (tmp_path / "s.agents").exists()

    def test_a_missing_session_id_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        ledger.handle_track_agents({"tool_name": "Agent", "tool_input": {}})
        assert not (tmp_path / "s.agents").exists()

    def test_an_unresolvable_agent_id_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        monkeypatch.setattr(ledger, "_newest_task_agent_id", lambda: "")
        ledger.handle_track_agents({"session_id": "s", "tool_name": "Agent", "tool_input": {}})
        assert not (tmp_path / "s.agents").exists()

    def test_a_repeat_dispatch_of_one_id_appends_once(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(router, "STATE_DIR", tmp_path)
        event = {"session_id": "s", "tool_name": "Agent", "tool_response": {"agentId": "a-1"}, "tool_input": {}}
        ledger.handle_track_agents(event)
        ledger.handle_track_agents(event)
        assert (tmp_path / "s.agents").read_text(encoding="utf-8").count("a-1") == 1

    def test_a_non_dict_response_carries_no_id(self) -> None:
        assert ledger._agent_id_from_response("not-a-dict") == ""
        assert ledger._agent_id_from_response({"agentId": "   "}) == ""


class TestPostToolUseWireRegistration:
    """The rename is only safe if a REAL router run still reaches the handler.

    Every assertion above imports the sibling directly, so they stay green against a
    module the router can no longer import — and an unimportable hook fails OPEN, so
    the capture would just stop with no error anywhere. This drives the shipped
    entry point as a subprocess and reads the state file it wrote.
    """

    def _run(self, tmp_path: Path, payload: dict) -> subprocess.CompletedProcess[str]:
        state_dir = {"T3_HOOK_STATE_DIR": str(tmp_path), "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(tmp_path)}
        return subprocess.run(
            [sys.executable, str(_HOOK_ROUTER), "--event", "PostToolUse"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env={**os.environ, **state_dir},
        )

    def test_a_dispatch_reaches_the_ledger_through_the_shipped_entry_point(self, tmp_path: Path) -> None:
        result = self._run(
            tmp_path,
            {
                "session_id": "wire",
                "tool_name": "Agent",
                "tool_response": {"agentId": "a-wire-1"},
                "tool_input": {"description": "wire probe"},
            },
        )
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "wire.agents").read_text(encoding="utf-8").splitlines() == ["a-wire-1\twire probe"]

    def test_a_non_agent_tool_writes_no_ledger(self, tmp_path: Path) -> None:
        # The control: the same wire, a payload the handler must ignore. Without it a
        # harness that wrote the file for ANY reason would read as a passing probe.
        result = self._run(tmp_path, {"session_id": "wire", "tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "wire.agents").exists()


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """A fresh interpreter imports the sibling without Django configured or teatree loaded."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import dispatch_ledger as r; "
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

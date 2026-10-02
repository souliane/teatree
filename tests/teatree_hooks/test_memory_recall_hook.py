"""UserPromptSubmit cold-tier memory recall hook (#2746).

The thin leaf ``hooks.scripts.memory_recall`` resolves the project memory dir from
the hook ``data`` and prints the recall block (stdout IS additionalContext on
UserPromptSubmit). Integration-leaning: a real cold tier under ``tmp_path`` reached
via a fake ``transcript_path``; the kill-switch path patches the config reader.
"""

from pathlib import Path

import pytest

from hooks.scripts import memory_recall
from hooks.scripts.hook_router import _HANDLERS
from hooks.scripts.memory_recall import handle_recall_cold_memory

_COLD_HEADER = "# Auto Memory — Cold Archive Index\n\n> preamble.\n\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project layout: ``<tmp>/project/<session>.jsonl`` + a sibling ``memory/`` cold tier."""
    proj = tmp_path / "project"
    memory = proj / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY_ARCHIVE.md").write_text(
        _COLD_HEADER + "- feedback_worktree_first.md — always create a worktree before editing project files\n",
        encoding="utf-8",
    )
    return proj


def _data(proj: Path, prompt: str) -> dict:
    return {"transcript_path": str(proj / "session.jsonl"), "prompt": prompt, "cwd": str(proj)}


class TestHandlerInjection:
    def test_prints_recall_block_for_relevant_prompt(self, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
        handle_recall_cold_memory(_data(project, "how do I create a worktree before editing the project files?"))
        out = capsys.readouterr().out
        assert "feedback_worktree_first.md" in out
        assert "Relevant archived memory rules" in out

    def test_prints_nothing_for_unrelated_prompt(self, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
        handle_recall_cold_memory(_data(project, "an unrelated question about quantum chromodynamics"))
        assert capsys.readouterr().out == ""

    def test_prints_nothing_when_transcript_path_absent_and_no_cwd_match(
        self, project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No transcript_path and a cwd whose project-slug dir does not exist under
        # a tmp HOME -> no resolvable cold index -> inject nothing.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: project.parent))
        data = {"prompt": "create a worktree before editing project files", "cwd": "/no/such/place"}
        handle_recall_cold_memory(data)
        assert capsys.readouterr().out == ""

    def test_prints_nothing_when_disabled_via_kill_switch(
        self, project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(memory_recall, "_memory_recall_enabled", lambda: False)
        handle_recall_cold_memory(_data(project, "create a worktree before editing project files"))
        assert capsys.readouterr().out == ""

    def test_non_dict_data_is_a_silent_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_recall_cold_memory("not a dict")
        assert capsys.readouterr().out == ""


class TestCwdFallbackResolution:
    def test_resolves_memory_dir_from_cwd_slug_when_no_transcript(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The harness names the project dir from the cwd with `/` -> `-`.
        cwd = "/Users/dev/workspace/proj"
        slug = cwd.replace("/", "-")
        memory = tmp_path / ".claude" / "projects" / slug / "memory"
        memory.mkdir(parents=True)
        (memory / "MEMORY_ARCHIVE.md").write_text(
            _COLD_HEADER + "- feedback_worktree_first.md — always create a worktree before editing project files\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        handle_recall_cold_memory({"prompt": "create a worktree before editing project files", "cwd": cwd})
        assert "feedback_worktree_first.md" in capsys.readouterr().out


class TestHandlerRegistration:
    def test_handler_registered_last_on_user_prompt_submit(self) -> None:
        handlers = _HANDLERS["UserPromptSubmit"]
        assert handle_recall_cold_memory in handlers
        assert handlers[-1] is handle_recall_cold_memory

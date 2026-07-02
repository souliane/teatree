"""Tests for the BLUEPRINT-sync commit-msg hook (souliane/teatree#8).

The hook fails when ``src/`` changes without a corresponding BLUEPRINT update,
unless the commit type is exempt (test/docs/style/chore/ci/fix/refactor). The
"BLUEPRINT" is the top-level ``BLUEPRINT.md`` plus its split appendix files
under ``docs/blueprint/`` — updating an appendix satisfies the requirement just
as the monolith does (teatree#2237: the appendices ARE the BLUEPRINT).

The exemption depends on the hook reading the *commit message*. The hook must
therefore source the commit type robustly — from the commit-message file git
hands it at the ``commit-msg`` stage, and never from a staged source filename it
might be handed at another invocation (pre-commit stage / ``prek run
--all-files``). The latter coupling is the bug behind task #35: a positional
argument that is a ``src/`` path was mis-read as the commit message, so the
``fix:``/``refactor:`` exemption could never match and a ``fix(db)`` commit was
gated.
"""

from pathlib import Path

import pytest

from scripts.hooks import check_blueprint_sync as hook


class TestIsBlueprint:
    @pytest.mark.parametrize(
        "path",
        [
            "BLUEPRINT.md",
            "docs/blueprint/configuration.md",
            "docs/blueprint/loop-topology.md",
            "docs/blueprint/factory-architecture.md",
        ],
    )
    def test_blueprint_paths_count(self, path: str) -> None:
        assert hook._is_blueprint(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "docs/dependency-graph.md",
            "docs/blueprint/notes.txt",
            "src/teatree/config_agent.py",
            "README.md",
            "docs/blueprintish.md",
        ],
    )
    def test_non_blueprint_paths_do_not_count(self, path: str) -> None:
        assert hook._is_blueprint(path) is False


class TestLooksLikeCommitMsgFile:
    """A commit-message file is distinguished from a staged source filename.

    The commit-type source must read git's commit-message file, never a staged
    source filename a non-commit-msg invocation might hand the hook as argv[1].
    """

    @pytest.mark.parametrize(
        "path",
        [
            ".git/COMMIT_EDITMSG",
            "/repo/.git/COMMIT_EDITMSG",
            "/repo/.git/worktrees/wt/COMMIT_EDITMSG",
            ".git/MERGE_MSG",
        ],
    )
    def test_commit_msg_files_are_recognized(self, path: str) -> None:
        assert hook._looks_like_commit_msg_file(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/teatree/config_agent.py",
            "scripts/hooks/check_blueprint_sync.py",
            "BLUEPRINT.md",
            "docs/blueprint/configuration.md",
            "tests/test_check_blueprint_sync_hook.py",
        ],
    )
    def test_source_filenames_are_not_commit_msg_files(self, path: str) -> None:
        assert hook._looks_like_commit_msg_file(path) is False


class TestCommitMessage:
    def test_reads_message_from_commit_msg_argv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text("fix(db): a thing\n", encoding="utf-8")
        monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py", str(msg_file)])
        assert hook._commit_message() == "fix(db): a thing"

    def test_ignores_staged_source_filename_argv_and_falls_back_to_git_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The regression: a non-commit-msg argv (a staged src path, as handed at
        # the pre-commit stage / `prek run --all-files`) must NOT be read as the
        # commit message. The hook falls back to git's canonical COMMIT_EDITMSG.
        src_file = tmp_path / "src" / "teatree" / "foo.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("import pathlib\n", encoding="utf-8")

        canonical = tmp_path / "COMMIT_EDITMSG"
        canonical.write_text("fix(db): a thing\n", encoding="utf-8")
        monkeypatch.setattr(hook, "_git_commit_editmsg_path", lambda: str(canonical))

        monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py", "src/teatree/foo.py"])
        # Must read the real commit message from git's canonical path, not the
        # first line of the staged source file.
        assert hook._commit_message() == "fix(db): a thing"

    def test_no_argv_falls_back_to_git_canonical_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        canonical = tmp_path / "COMMIT_EDITMSG"
        canonical.write_text("refactor(core): extract helper\n", encoding="utf-8")
        monkeypatch.setattr(hook, "_git_commit_editmsg_path", lambda: str(canonical))
        monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py"])
        assert hook._commit_message() == "refactor(core): extract helper"

    def test_missing_message_everywhere_returns_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(hook, "_git_commit_editmsg_path", lambda: str(tmp_path / "nope"))
        monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py"])
        assert hook._commit_message() == ""


class TestMain:
    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        message: str,
        staged: list[str],
        argv_is_src: bool = False,
    ) -> int:
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text(message + "\n", encoding="utf-8")
        # Git's canonical commit-msg path always carries the real message.
        monkeypatch.setattr(hook, "_git_commit_editmsg_path", lambda: str(msg_file))
        if argv_is_src:
            # Simulate the buggy invocation: a staged src path as argv[1].
            monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py", "src/teatree/x.py"])
        else:
            monkeypatch.setattr(hook.sys, "argv", ["check_blueprint_sync.py", str(msg_file)])
        monkeypatch.setattr(hook, "_staged_files", lambda: staged)
        return hook.main()

    def test_src_without_blueprint_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(agent): something",
            staged=["src/teatree/config_agent.py"],
        )
        assert rc == 1

    def test_src_with_top_level_blueprint_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(agent): something",
            staged=["src/teatree/config_agent.py", "BLUEPRINT.md"],
        )
        assert rc == 0

    def test_src_with_appendix_blueprint_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Documenting in a docs/blueprint/ appendix satisfies the sync gate, so a
        # feat commit need not touch BLUEPRINT.md.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(agent): single-toggle model pin override",
            staged=["src/teatree/config_agent.py", "docs/blueprint/configuration.md"],
        )
        assert rc == 0

    def test_exempt_commit_type_passes_without_blueprint(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="fix(agent): a bug",
            staged=["src/teatree/config_agent.py"],
        )
        assert rc == 0

    def test_refactor_commit_type_passes_without_blueprint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="refactor(core): extract helper",
            staged=["src/teatree/config_agent.py"],
        )
        assert rc == 0

    def test_no_src_change_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(docs): docs only",
            staged=["docs/blueprint/configuration.md"],
        )
        assert rc == 0

    def test_fix_commit_exempt_even_when_argv_is_staged_src_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Task #35 regression: when the hook is handed a staged src path as
        # argv[1] (pre-commit stage / `prek run --all-files`) instead of the
        # commit-message file, the fix: exemption must STILL fire by sourcing
        # the commit type from git's canonical COMMIT_EDITMSG.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="fix(db): reconcile renumbered migration records",
            staged=["src/teatree/db.py"],
            argv_is_src=True,
        )
        assert rc == 0

    def test_feat_commit_still_gated_when_argv_is_staged_src_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The fix must not over-correct: a feat commit needing a BLUEPRINT
        # update is still gated even when argv[1] is a staged src path, because
        # the commit type is sourced from git's canonical COMMIT_EDITMSG.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(db): a new capability",
            staged=["src/teatree/db.py"],
            argv_is_src=True,
        )
        assert rc == 1

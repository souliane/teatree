"""Tests for the BLUEPRINT-sync commit-msg hook (souliane/teatree#8).

The hook fails when ``src/`` changes without a corresponding BLUEPRINT update,
unless the commit type is exempt (test/docs/style/chore/ci/fix). The
"BLUEPRINT" is the top-level ``BLUEPRINT.md`` plus its split appendix files
under ``docs/blueprint/`` — updating an appendix satisfies the requirement just
as the monolith does (teatree#2237: the appendices ARE the BLUEPRINT).
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


class TestMain:
    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        message: str,
        staged: list[str],
    ) -> int:
        msg_file = tmp_path / "COMMIT_EDITMSG"
        msg_file.write_text(message + "\n", encoding="utf-8")
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
        # The key teatree#2237 case: documenting in a docs/blueprint/ appendix
        # satisfies the sync gate, so a feat commit need not touch BLUEPRINT.md.
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(agent): single-toggle Fable kill-switch",
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

    def test_no_src_change_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        rc = self._run(
            monkeypatch,
            tmp_path,
            message="feat(docs): docs only",
            staged=["docs/blueprint/configuration.md"],
        )
        assert rc == 0

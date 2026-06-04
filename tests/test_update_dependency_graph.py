"""Tests for the tach dependency-graph update hook.

Verifies that the generator writes the mermaid diagram to the dedicated
generated file rather than into BLUEPRINT.md, so that structural changes
that regenerate the graph do not grow the BLUEPRINT byte-budget corpus.
"""

from pathlib import Path
from unittest import mock

import pytest

from scripts.hooks import update_dependency_graph as hook


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hook, "_repo_root", lambda: tmp_path)
    blueprint = tmp_path / "BLUEPRINT.md"
    blueprint.write_text(
        "# BLUEPRINT\n\nSome prose.\n\n## Module Dependency Graph\n\n"
        "See [docs/dependency-graph.md](docs/dependency-graph.md) for the auto-generated graph.\n",
        encoding="utf-8",
    )
    return tmp_path


class TestDependencyGraphWritesToDedicatedFile:
    def test_graph_written_to_dedicated_file_not_blueprint(
        self,
        fake_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mermaid = "graph TD\n    A --> B"
        monkeypatch.setattr(hook, "_generate_mermaid", lambda: mermaid)
        monkeypatch.setattr(hook.subprocess, "run", mock.Mock(return_value=mock.Mock()))

        result = hook.main()

        assert result == 0
        graph_file = fake_repo / hook._GRAPH_FILE
        assert graph_file.exists(), f"{hook._GRAPH_FILE} was not created"
        content = graph_file.read_text(encoding="utf-8")
        assert "graph TD" in content
        assert "A --> B" in content

    def test_blueprint_does_not_contain_mermaid_after_update(
        self,
        fake_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mermaid = "graph TD\n    X --> Y"
        monkeypatch.setattr(hook, "_generate_mermaid", lambda: mermaid)
        monkeypatch.setattr(hook.subprocess, "run", mock.Mock(return_value=mock.Mock()))

        hook.main()

        blueprint = (fake_repo / "BLUEPRINT.md").read_text(encoding="utf-8")
        assert "graph TD" not in blueprint, "mermaid diagram must NOT appear in BLUEPRINT.md"
        assert "X --> Y" not in blueprint

    def test_blueprint_retains_link_pointer(
        self,
        fake_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(hook, "_generate_mermaid", lambda: "graph TD\n    A --> B")
        monkeypatch.setattr(hook.subprocess, "run", mock.Mock(return_value=mock.Mock()))

        hook.main()

        blueprint = (fake_repo / "BLUEPRINT.md").read_text(encoding="utf-8")
        assert "dependency-graph.md" in blueprint, "BLUEPRINT.md must link to the generated graph file"

    def test_blueprint_size_unchanged_when_graph_grows(
        self,
        fake_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Adding tach structure that grows the mermaid must not grow BLUEPRINT.md."""
        blueprint = fake_repo / "BLUEPRINT.md"
        size_before = blueprint.stat().st_size

        large_mermaid = "graph TD\n" + "\n".join(f"    M{i} --> M{i + 1}" for i in range(50))
        monkeypatch.setattr(hook, "_generate_mermaid", lambda: large_mermaid)
        monkeypatch.setattr(hook.subprocess, "run", mock.Mock(return_value=mock.Mock()))

        hook.main()

        size_after = blueprint.stat().st_size
        assert size_after == size_before, (
            f"BLUEPRINT.md grew from {size_before} to {size_after} bytes — "
            "the mermaid graph must live in the dedicated file, not BLUEPRINT.md"
        )

    def test_no_mermaid_output_is_noop(
        self,
        fake_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(hook, "_generate_mermaid", lambda: "")
        monkeypatch.setattr(hook.subprocess, "run", mock.Mock(return_value=mock.Mock()))

        result = hook.main()

        assert result == 0
        graph_file = fake_repo / hook._GRAPH_FILE
        assert not graph_file.exists()

    def test_graph_anchored_to_repo_root_not_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The graph is written under the repo root even when CWD differs."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setattr(hook, "_repo_root", lambda: repo_root)
        monkeypatch.chdir(elsewhere)
        monkeypatch.setattr(hook, "_generate_mermaid", lambda: "graph TD\n    A --> B")
        git_add = mock.Mock(return_value=mock.Mock())
        monkeypatch.setattr(hook.subprocess, "run", git_add)

        hook.main()

        assert (repo_root / hook._GRAPH_FILE).exists(), "graph must be written under the repo root"
        assert not (elsewhere / hook._GRAPH_FILE).exists(), "graph must NOT be written relative to CWD"
        git_add_arg = git_add.call_args.args[0]
        assert git_add_arg[:2] == ["git", "add"]
        assert Path(git_add_arg[2]).is_absolute(), "git add path must be repo-root-anchored, not a CWD-relative path"


class TestRealBlueprintHasNoMermaid:
    """The committed BLUEPRINT.md must not contain the mermaid diagram."""

    def test_blueprint_contains_no_mermaid_block(self) -> None:
        repo_root = hook._repo_root()
        blueprint = (repo_root / "BLUEPRINT.md").read_text(encoding="utf-8")
        assert "<!-- tach-dependency-graph:start -->" not in blueprint, (
            "BLUEPRINT.md still contains the tach-dependency-graph markers — "
            "the mermaid diagram must be moved to docs/dependency-graph.md"
        )

    def test_dedicated_graph_file_exists(self) -> None:
        repo_root = hook._repo_root()
        graph_file = repo_root / hook._GRAPH_FILE
        assert graph_file.exists(), f"The dedicated dependency graph file {hook._GRAPH_FILE!r} does not exist"

    def test_dedicated_graph_file_contains_mermaid(self) -> None:
        repo_root = hook._repo_root()
        graph_file = repo_root / hook._GRAPH_FILE
        assert graph_file.exists(), f"The dedicated dependency graph file {hook._GRAPH_FILE!r} does not exist"
        content = graph_file.read_text(encoding="utf-8")
        assert "```mermaid" in content, f"{hook._GRAPH_FILE} exists but contains no mermaid block"

"""Tests for the BLUEPRINT corpus size-budget gate (#1128).

The gate enforces that the BLUEPRINT stays architectural rather than a
prose mirror of the code. It fails the commit when the corpus exceeds
the per-file and total byte budgets; ``BLUEPRINT_SIZE_OVERRIDE=1`` is
the documented escape hatch.
"""

import importlib
from pathlib import Path
from unittest import mock

import pytest

from scripts.hooks import check_blueprint_size_budget as gate


def _write(repo: Path, relpath: str, content: str) -> None:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root with the gate pointed at it."""
    monkeypatch.setattr(gate, "_repo_root", lambda: tmp_path)
    # Every test exercises the path where BLUEPRINT.md is in the commit,
    # so the gate runs its budget check.
    monkeypatch.setattr(gate, "_blueprint_touched", lambda: True)
    return tmp_path


class TestBudgetEnforcement:
    def test_zero_corpus_passes(self, fake_repo: Path) -> None:
        # No BLUEPRINT yet, no appendix dir — gate is a no-op.
        assert gate.main() == 0

    def test_under_budget_passes(self, fake_repo: Path) -> None:
        _write(fake_repo, "BLUEPRINT.md", "x" * 1000)
        _write(fake_repo, "docs/blueprint/configuration.md", "y" * 500)
        assert gate.main() == 0

    def test_top_level_over_budget_fails(self, fake_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(fake_repo, "BLUEPRINT.md", "x" * (gate._BUDGET_TOP_LEVEL_BYTES + 1))
        assert gate.main() == 1
        captured = capsys.readouterr()
        assert "BLUEPRINT.md" in captured.err
        assert "budget" in captured.err

    def test_appendix_over_budget_fails(self, fake_repo: Path) -> None:
        _write(fake_repo, "BLUEPRINT.md", "x" * 100)
        _write(
            fake_repo,
            "docs/blueprint/factory-architecture.md",
            "y" * (gate._BUDGET_APPENDICES_BYTES + 1),
        )
        assert gate.main() == 1

    def test_total_corpus_over_budget_fails(self, fake_repo: Path) -> None:
        # Each individual file under its budget, but combined they bust
        # the total — exercises the third breach branch. Sizes are derived
        # from the constants so the pair lands exactly at the total budget,
        # then a little extra corpus tips it over without breaching either
        # per-file budget.
        top_fill = gate._BUDGET_TOP_LEVEL_BYTES
        appendix_fill = gate._BUDGET_TOTAL_BYTES - top_fill
        assert appendix_fill <= gate._BUDGET_APPENDICES_BYTES
        _write(fake_repo, "BLUEPRINT.md", "x" * top_fill)
        _write(fake_repo, "docs/blueprint/configuration.md", "y" * appendix_fill)
        assert gate.main() == 0
        # Add more corpus to push the combined total past the cap while each
        # individual file stays under its own budget.
        _write(fake_repo, "docs/blueprint/loop-topology.md", "z" * 100)
        assert gate.main() == 1


class TestEscapeHatches:
    def test_env_override_skips_check(self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(
            fake_repo,
            "BLUEPRINT.md",
            "x" * (gate._BUDGET_TOP_LEVEL_BYTES * 10),
        )
        monkeypatch.setenv("BLUEPRINT_SIZE_OVERRIDE", "1")
        assert gate.main() == 0

    def test_no_blueprint_in_commit_skips_check(
        self,
        fake_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An over-budget corpus is fine when the commit doesn't touch it.
        _write(
            fake_repo,
            "BLUEPRINT.md",
            "x" * (gate._BUDGET_TOP_LEVEL_BYTES * 10),
        )
        monkeypatch.setattr(gate, "_blueprint_touched", lambda: False)
        assert gate.main() == 0


class TestBlueprintTouchedDetection:
    """``_blueprint_touched()`` reads ``git diff --cached`` output."""

    def test_blueprint_file_in_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = mock.Mock(stdout="src/foo.py\nBLUEPRINT.md\n")
        monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: fake)
        assert gate._blueprint_touched() is True

    def test_appendix_file_in_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = mock.Mock(stdout="docs/blueprint/configuration.md\n")
        monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: fake)
        assert gate._blueprint_touched() is True

    def test_unrelated_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = mock.Mock(stdout="src/teatree/foo.py\ntests/test_foo.py\n")
        monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: fake)
        assert gate._blueprint_touched() is False


class TestRepoRoot:
    """``_repo_root()`` resolves the gate file's grandparent."""

    def test_resolves_to_repo_root(self) -> None:
        importlib.reload(gate)
        # The gate lives in scripts/hooks/, so root is two parents up.
        assert (gate._repo_root() / "BLUEPRINT.md").exists()

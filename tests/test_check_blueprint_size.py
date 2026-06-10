"""Tests for the BLUEPRINT.md hard size cap (#1180).

The gate hard-fails when ``BLUEPRINT.md`` exceeds ``gate._THRESHOLD_BYTES``
(the single source of truth for the cap, so this docstring cannot drift). The
documented escape hatch is ``T3_BLUEPRINT_SIZE_OVERRIDE=1`` for intentional,
reviewed growth.
"""

from pathlib import Path

import pytest

from scripts.hooks import check_blueprint_size as gate


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Throwaway repo root with the gate pointed at it."""
    monkeypatch.setattr(gate, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv(gate._OVERRIDE_ENV_VAR, raising=False)
    return tmp_path


class TestSizeCap:
    def test_small_file_passes(self, fake_repo: Path) -> None:
        (fake_repo / "BLUEPRINT.md").write_text("x" * 1000, encoding="utf-8")
        assert gate.main() == 0

    def test_at_threshold_passes(self, fake_repo: Path) -> None:
        (fake_repo / "BLUEPRINT.md").write_text("x" * gate._THRESHOLD_BYTES, encoding="utf-8")
        assert gate.main() == 0

    def test_over_threshold_fails(self, fake_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (fake_repo / "BLUEPRINT.md").write_text("x" * (gate._THRESHOLD_BYTES + 1), encoding="utf-8")
        assert gate.main() == 1
        captured = capsys.readouterr()
        assert "BLUEPRINT.md" in captured.err
        assert "threshold" in captured.err
        assert gate._OVERRIDE_ENV_VAR in captured.err

    def test_missing_file_passes(self, fake_repo: Path) -> None:
        assert gate.main() == 0


class TestOverride:
    def test_env_override_skips_check(self, fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (fake_repo / "BLUEPRINT.md").write_text("x" * (gate._THRESHOLD_BYTES * 5), encoding="utf-8")
        monkeypatch.setenv(gate._OVERRIDE_ENV_VAR, "1")
        assert gate.main() == 0

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "true"])
    def test_non_one_env_value_does_not_override(
        self,
        fake_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        # Strict "1" is the only override token — mirrors the sibling
        # #1128 gate so neither hook silently accepts "0"/"false".
        (fake_repo / "BLUEPRINT.md").write_text("x" * (gate._THRESHOLD_BYTES + 1), encoding="utf-8")
        monkeypatch.setenv(gate._OVERRIDE_ENV_VAR, value)
        assert gate.main() == 1


class TestRepoRoot:
    def test_resolves_to_actual_repo_root(self) -> None:
        assert (gate._repo_root() / "BLUEPRINT.md").exists()

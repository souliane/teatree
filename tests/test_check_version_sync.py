"""The manifest version-sync gate (``scripts/hooks/check_version_sync.py``).

A manifest that ships in the repo but declares no version was dropped before the
comparison, so the gate went green on the one state it exists to catch: a release
bump that reached ``pyproject.toml`` and left ``plugin.json`` or ``apm.yml``
behind. A manifest the repo does not ship at all is still out of scope.
"""

import json
from pathlib import Path

import pytest

from scripts.hooks import check_version_sync


def _write_manifests(
    root: Path,
    *,
    pyproject: str | None = "1.2.3",
    plugin: str | None = "1.2.3",
    apm: str | None = "1.2.3",
) -> None:
    if pyproject is not None:
        (root / "pyproject.toml").write_text(f'[project]\nversion = "{pyproject}"\n', encoding="utf-8")
    if plugin is not None:
        (root / ".claude-plugin").mkdir(exist_ok=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": plugin}), encoding="utf-8")
    if apm is not None:
        (root / "apm.yml").write_text(f"name: demo\nversion: {apm}\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(check_version_sync, "ROOT", tmp_path)
    return tmp_path


class TestVersionSyncGate:
    def test_all_three_in_sync_passes(self, repo: Path) -> None:
        _write_manifests(repo)

        assert check_version_sync.main() == 0

    def test_mismatch_fails(self, repo: Path) -> None:
        _write_manifests(repo, plugin="9.9.9")

        assert check_version_sync.main() == 1

    def test_shipped_manifest_declaring_no_version_fails(
        self,
        repo: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_manifests(repo, plugin=None)
        (repo / ".claude-plugin").mkdir()
        (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "t3"}), encoding="utf-8")

        assert check_version_sync.main() == 1
        assert "plugin.json" in capsys.readouterr().out

    def test_apm_without_a_version_line_fails(self, repo: Path) -> None:
        _write_manifests(repo, apm=None)
        (repo / "apm.yml").write_text("name: demo\n", encoding="utf-8")

        assert check_version_sync.main() == 1

    def test_absent_manifest_is_out_of_scope(self, repo: Path) -> None:
        _write_manifests(repo, plugin=None, apm=None)

        assert check_version_sync.main() == 0

    def test_real_repo_manifests_are_in_sync(self) -> None:
        assert check_version_sync.main() == 0

"""Tests for `t3 setup --write-automode` consent gating (#3408).

The full ``t3 setup`` callback does heavy clone-resolution/tool-install work, so
these exercise the two units that implement the flag —
``_write_automode_consented`` and ``_maybe_write_managed_settings`` — directly.
The merge itself is covered by ``test_claude_settings_merge``; here the concern
is that teatree writes the user's settings ONLY on explicit consent.
"""

import json
from pathlib import Path

from teatree.cli.setup.command import _maybe_write_managed_settings, _write_automode_consented


class TestConsent:
    def test_yes_flag_consents(self) -> None:
        assert _write_automode_consented(yes=True) is True

    def test_no_consent_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("TEATREE_WRITE_AUTOMODE", raising=False)
        assert _write_automode_consented(yes=False) is False

    def test_env_consent(self, monkeypatch) -> None:
        monkeypatch.setenv("TEATREE_WRITE_AUTOMODE", "1")
        assert _write_automode_consented(yes=False) is True

    def test_env_falsey_does_not_consent(self, monkeypatch) -> None:
        monkeypatch.setenv("TEATREE_WRITE_AUTOMODE", "0")
        assert _write_automode_consented(yes=False) is False


def _repo_with_template(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "deploy" / "claude-settings.template.json").write_text(
        json.dumps({"model": "m", "autoMode": {"allow": ["grant"]}}),
        encoding="utf-8",
    )
    return repo


class TestMaybeWriteManagedSettings:
    def test_noop_without_flag(self, tmp_path: Path) -> None:
        repo = _repo_with_template(tmp_path)
        target = tmp_path / "settings.json"
        _maybe_write_managed_settings(repo, target, write_automode=False, yes=True)
        assert not target.exists()

    def test_noop_without_consent(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("TEATREE_WRITE_AUTOMODE", raising=False)
        repo = _repo_with_template(tmp_path)
        target = tmp_path / "settings.json"
        _maybe_write_managed_settings(repo, target, write_automode=True, yes=False)
        assert not target.exists()

    def test_writes_on_flag_and_consent(self, tmp_path: Path) -> None:
        repo = _repo_with_template(tmp_path)
        target = tmp_path / "settings.json"
        _maybe_write_managed_settings(repo, target, write_automode=True, yes=True)
        assert json.loads(target.read_text())["autoMode"]["allow"] == ["grant"]

    def test_missing_template_is_graceful(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / "deploy").mkdir(parents=True)  # no template file
        target = tmp_path / "settings.json"
        _maybe_write_managed_settings(repo, target, write_automode=True, yes=True)
        assert not target.exists()

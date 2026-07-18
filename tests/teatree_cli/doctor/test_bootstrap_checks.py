"""Tests for the fresh-box bootstrap-hardening doctor checks (#3405/#3409/#3410)."""

import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.cli.doctor.checks_bootstrap import (
    _check_claude_settings_drift,
    _check_gh_token_permissions,
    _check_provision_concurrency_from_host,
    _slug_from_repo_url,
)
from teatree.core.gates.gh_token_preflight import GhTokenProbe
from teatree.core.models.config_setting import ConfigSetting


class TestSlugFromRepoUrl:
    def test_https(self) -> None:
        assert _slug_from_repo_url("https://github.com/souliane/teatree.git") == "souliane/teatree"

    def test_ssh(self) -> None:
        assert _slug_from_repo_url("git@github.com:souliane/teatree.git") == "souliane/teatree"

    def test_non_github(self) -> None:
        assert _slug_from_repo_url("https://gitlab.com/x/y.git") is None


class TestGhTokenPermissionsCheck:
    def test_skips_when_no_slug(self) -> None:
        with patch("teatree.cli.doctor.checks_bootstrap._resolve_repo_slug", return_value=None):
            assert _check_gh_token_permissions() is True

    def test_passes_when_token_has_every_permission(self) -> None:
        with (
            patch("teatree.cli.doctor.checks_bootstrap._resolve_repo_slug", return_value="o/r"),
            patch(
                "teatree.core.gates.gh_token_preflight.probe_token_permissions",
                return_value=GhTokenProbe(missing=()),
            ),
        ):
            assert _check_gh_token_permissions() is True

    def test_fails_loud_on_missing_permission(self, capsys) -> None:
        with (
            patch("teatree.cli.doctor.checks_bootstrap._resolve_repo_slug", return_value="o/r"),
            patch(
                "teatree.core.gates.gh_token_preflight.probe_token_permissions",
                return_value=GhTokenProbe(missing=("issues: write",)),
            ),
        ):
            ok = _check_gh_token_permissions()
        assert ok is False
        assert "issues: write" in capsys.readouterr().out

    def test_indeterminate_probe_skips(self) -> None:
        with (
            patch("teatree.cli.doctor.checks_bootstrap._resolve_repo_slug", return_value="o/r"),
            patch(
                "teatree.core.gates.gh_token_preflight.probe_token_permissions",
                return_value=GhTokenProbe(missing=(), indeterminate_reason="API unreachable"),
            ),
        ):
            assert _check_gh_token_permissions() is True


class TestProvisionConcurrencyFromHost(TestCase):
    def test_clears_stale_pin_below_host_auto(self) -> None:
        ConfigSetting.objects.set_value("provision_max_concurrency", 1)
        with patch("teatree.utils.ram_probe.default_provision_concurrency", return_value=4):
            ok = _check_provision_concurrency_from_host()
        assert ok is True
        # The stale small-box pin is cleared so the runtime auto-derives.
        assert ConfigSetting.objects.get_effective("provision_max_concurrency") is None

    def test_leaves_pin_at_or_above_host_auto(self) -> None:
        ConfigSetting.objects.set_value("provision_max_concurrency", 8)
        with patch("teatree.utils.ram_probe.default_provision_concurrency", return_value=4):
            _check_provision_concurrency_from_host()
        assert ConfigSetting.objects.get_effective("provision_max_concurrency") == 8

    def test_noop_when_unpinned(self) -> None:
        with patch("teatree.utils.ram_probe.default_provision_concurrency", return_value=4):
            assert _check_provision_concurrency_from_host() is True
        assert ConfigSetting.objects.get_effective("provision_max_concurrency") is None

    def test_apply_false_reports_without_clearing(self) -> None:
        ConfigSetting.objects.set_value("provision_max_concurrency", 1)
        with patch("teatree.utils.ram_probe.default_provision_concurrency", return_value=4):
            _check_provision_concurrency_from_host(apply=False)
        assert ConfigSetting.objects.get_effective("provision_max_concurrency") == 1


class TestClaudeSettingsDrift:
    def _stage(self, tmp_path: Path, template_payload: dict, target_payload: dict | None) -> tuple[Path, Path]:
        repo = tmp_path / "repo"
        (repo / "deploy").mkdir(parents=True)
        (repo / "deploy" / "claude-settings.template.json").write_text(json.dumps(template_payload), encoding="utf-8")
        home = tmp_path / "home"
        if target_payload is not None:
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / "settings.json").write_text(json.dumps(target_payload), encoding="utf-8")
        return repo, home

    def test_warns_on_drift(self, tmp_path: Path, capsys, monkeypatch) -> None:
        repo, home = self._stage(tmp_path, {"model": "new"}, {"model": "old"})
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        with patch("teatree.cli.doctor.checks_bootstrap._teatree_repo_root", return_value=repo):
            ok = _check_claude_settings_drift()
        assert ok is True  # surfacing-only, never gates
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "model" in out

    def test_silent_when_aligned(self, tmp_path: Path, capsys, monkeypatch) -> None:
        repo, home = self._stage(tmp_path, {"model": "same"}, {"model": "same", "statusLine": {"x": 1}})
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        with patch("teatree.cli.doctor.checks_bootstrap._teatree_repo_root", return_value=repo):
            _check_claude_settings_drift()
        assert "WARN" not in capsys.readouterr().out

    def test_skips_when_template_absent(self, tmp_path: Path, monkeypatch) -> None:
        repo = tmp_path / "repo"
        (repo / "deploy").mkdir(parents=True)  # no template
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
        with patch("teatree.cli.doctor.checks_bootstrap._teatree_repo_root", return_value=repo):
            assert _check_claude_settings_drift() is True

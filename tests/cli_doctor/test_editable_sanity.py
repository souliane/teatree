"""``DoctorService.check_editable_sanity`` — contribute/editable reconciliation.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.cli.doctor import DoctorService, IntrospectionHelpers
from teatree.core.models import ConfigSetting

from ._shared import _editable_map, _fake_entry_point, _stage_home, _write_teatree_toml


class TestCheckEditableSanity(TestCase):
    """End-to-end sanity check wired to a real ``~/.teatree.toml``.

    ``editable_info`` and ``entry_points`` are the two external boundaries
    we cannot make real without installing actual packages, so they stay as
    mocks. Everything else (config loading, repo discovery) runs live.

    ``contribute`` is a DB-home setting (#1775), so it is staged through the
    ``ConfigSetting`` store rather than a ``[teatree]`` TOML value (which is
    ignored on read). ``contribute=false`` is the resolved default, so those
    cases need no DB row — only an empty ``~/.teatree.toml`` for the parts of
    config loading that still touch the file.
    """

    @pytest.fixture(autouse=True)
    def _fixtures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_empty_when_contribute_false_and_nothing_editable(self):
        _stage_home(self.tmp_path, self.monkeypatch)
        _write_teatree_toml(self.tmp_path / ".teatree.toml", "[teatree]\n")

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")):
            assert DoctorService.check_editable_sanity() == []

    def test_empty_when_contribute_true_and_all_editable(self):
        _stage_home(self.tmp_path, self.monkeypatch)
        _write_teatree_toml(self.tmp_path / ".teatree.toml", "[teatree]\n")
        ConfigSetting.objects.set_value("contribute", value=True)

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")):
            assert DoctorService.check_editable_sanity() == []

    def test_auto_fixes_teatree_when_contribute_true(self):
        _stage_home(self.tmp_path, self.monkeypatch)
        _write_teatree_toml(self.tmp_path / ".teatree.toml", "[teatree]\n")
        ConfigSetting.objects.set_value("contribute", value=True)
        teatree_repo = self.tmp_path / "repos" / "teatree"
        teatree_repo.mkdir(parents=True)
        (teatree_repo / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        self.monkeypatch.setenv("T3_REPO", str(teatree_repo))
        self.monkeypatch.chdir(self.tmp_path)

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(DoctorService, "make_editable") as mock_fix,
        ):
            problems = DoctorService.check_editable_sanity()

        mock_fix.assert_called_once_with("teatree", teatree_repo)
        assert problems == []

    def test_warns_when_contribute_true_and_teatree_repo_not_found(self):
        _stage_home(self.tmp_path, self.monkeypatch)
        _write_teatree_toml(self.tmp_path / ".teatree.toml", "[teatree]\n")
        ConfigSetting.objects.set_value("contribute", value=True)
        self.monkeypatch.delenv("T3_REPO", raising=False)
        self.monkeypatch.chdir(self.tmp_path)

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch("teatree.find_project_root", return_value=None),
        ):
            problems = DoctorService.check_editable_sanity()

        assert any("contribute=true" in p for p in problems)

    def test_warns_when_teatree_editable_but_contribute_false(self):
        _stage_home(self.tmp_path, self.monkeypatch)
        _write_teatree_toml(self.tmp_path / ".teatree.toml", "[teatree]\n")

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")):
            problems = DoctorService.check_editable_sanity()

        assert any("contribute=false" in p for p in problems)

    def test_teatree_editable_warning_does_not_scold_contributor_setup(self):
        _stage_home(self.tmp_path, self.monkeypatch)
        _write_teatree_toml(self.tmp_path / ".teatree.toml", "[teatree]\n")

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")):
            problems = DoctorService.check_editable_sanity()

        msg = next(p for p in problems if "contribute=false" in p)
        assert "risk accidentally modifying" not in msg
        assert "contribute=true" in msg

    def test_auto_fixes_overlay_when_contribute_true(self):
        _stage_home(self.tmp_path, self.monkeypatch)
        _write_teatree_toml(self.tmp_path / ".teatree.toml", "[teatree]\n")
        # Overlay repos are discovered under config.clone_root(), resolved from
        # T3_WORKSPACE_DIR — not the retired [teatree] workspace_dir TOML key.
        self.monkeypatch.setenv("T3_WORKSPACE_DIR", str(self.tmp_path))
        ConfigSetting.objects.set_value("contribute", value=True)
        overlay_repo = self.tmp_path / "my-overlay"
        overlay_repo.mkdir()
        (overlay_repo / "pyproject.toml").write_text('[project]\nname = "my-overlay"\n')
        self.monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda **_kw: [_fake_entry_point("my-overlay")],
        )

        with (
            patch.object(
                IntrospectionHelpers,
                "editable_info",
                side_effect=_editable_map(teatree=(True, ""), **{"my-overlay": (False, "")}),
            ),
            patch.object(DoctorService, "make_editable") as mock_fix,
        ):
            problems = DoctorService.check_editable_sanity()

        mock_fix.assert_called_once_with("my-overlay", overlay_repo)
        assert problems == []

    def test_warns_when_overlay_editable_but_contribute_false(self):
        _stage_home(self.tmp_path, self.monkeypatch)
        _write_teatree_toml(self.tmp_path / ".teatree.toml", "[teatree]\n")
        self.monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda **_kw: [_fake_entry_point("my-overlay")],
        )

        with patch.object(
            IntrospectionHelpers,
            "editable_info",
            side_effect=_editable_map(teatree=(False, ""), **{"my-overlay": (True, "file:///src")}),
        ):
            problems = DoctorService.check_editable_sanity()

        assert any("contribute=false" in p for p in problems)

    def test_empty_when_all_states_align_with_contribute_false(self):
        _stage_home(self.tmp_path, self.monkeypatch)
        _write_teatree_toml(self.tmp_path / ".teatree.toml", "[teatree]\n")
        self.monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda **_kw: [_fake_entry_point("my-overlay")],
        )

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")):
            assert DoctorService.check_editable_sanity() == []

    def test_warns_when_overlay_repo_not_found(self):
        _stage_home(self.tmp_path, self.monkeypatch)
        _write_teatree_toml(self.tmp_path / ".teatree.toml", "[teatree]\n")
        # Overlay repos are discovered under config.clone_root(), resolved from
        # T3_WORKSPACE_DIR — not the retired [teatree] workspace_dir TOML key.
        self.monkeypatch.setenv("T3_WORKSPACE_DIR", str(self.tmp_path))
        ConfigSetting.objects.set_value("contribute", value=True)
        # No ``my-overlay`` directory under workspace_dir.
        self.monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda **_kw: [_fake_entry_point("my-overlay")],
        )

        with patch.object(
            IntrospectionHelpers,
            "editable_info",
            side_effect=_editable_map(teatree=(True, ""), **{"my-overlay": (False, "")}),
        ):
            problems = DoctorService.check_editable_sanity()

        assert any("overlay" in p and "repo not found" in p for p in problems)

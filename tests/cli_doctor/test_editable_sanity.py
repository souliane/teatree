"""``DoctorService.check_editable_sanity`` — contribute/editable reconciliation.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

from unittest.mock import patch

import pytest

from teatree.cli.doctor import DoctorService, IntrospectionHelpers
from teatree.core.models import ConfigSetting

from ._shared import _editable_map, _fake_entry_point, _stage_home, _write_teatree_toml


@pytest.mark.django_db
class TestCheckEditableSanity:
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

    def test_empty_when_contribute_false_and_nothing_editable(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\n")

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")):
            assert DoctorService.check_editable_sanity() == []

    def test_empty_when_contribute_true_and_all_editable(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\n")
        ConfigSetting.objects.set_value("contribute", value=True)

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")):
            assert DoctorService.check_editable_sanity() == []

    def test_auto_fixes_teatree_when_contribute_true(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\n")
        ConfigSetting.objects.set_value("contribute", value=True)
        teatree_repo = tmp_path / "repos" / "teatree"
        teatree_repo.mkdir(parents=True)
        (teatree_repo / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        monkeypatch.setenv("T3_REPO", str(teatree_repo))
        monkeypatch.chdir(tmp_path)

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch.object(DoctorService, "make_editable") as mock_fix,
        ):
            problems = DoctorService.check_editable_sanity()

        mock_fix.assert_called_once_with("teatree", teatree_repo)
        assert problems == []

    def test_warns_when_contribute_true_and_teatree_repo_not_found(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\n")
        ConfigSetting.objects.set_value("contribute", value=True)
        monkeypatch.delenv("T3_REPO", raising=False)
        monkeypatch.chdir(tmp_path)

        with (
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
            patch("teatree.find_project_root", return_value=None),
        ):
            problems = DoctorService.check_editable_sanity()

        assert any("contribute=true" in p for p in problems)

    def test_warns_when_teatree_editable_but_contribute_false(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\n")

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")):
            problems = DoctorService.check_editable_sanity()

        assert any("contribute=false" in p for p in problems)

    def test_teatree_editable_warning_does_not_scold_contributor_setup(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\n")

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")):
            problems = DoctorService.check_editable_sanity()

        msg = next(p for p in problems if "contribute=false" in p)
        assert "risk accidentally modifying" not in msg
        assert "contribute=true" in msg

    def test_auto_fixes_overlay_when_contribute_true(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            f'[teatree]\nworkspace_dir = "{tmp_path}"\n',
        )
        ConfigSetting.objects.set_value("contribute", value=True)
        overlay_repo = tmp_path / "my-overlay"
        overlay_repo.mkdir()
        (overlay_repo / "pyproject.toml").write_text('[project]\nname = "my-overlay"\n')
        monkeypatch.setattr(
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

    def test_warns_when_overlay_editable_but_contribute_false(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\n")
        monkeypatch.setattr(
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

    def test_empty_when_all_states_align_with_contribute_false(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(tmp_path / ".teatree.toml", "[teatree]\n")
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda **_kw: [_fake_entry_point("my-overlay")],
        )

        with patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")):
            assert DoctorService.check_editable_sanity() == []

    def test_warns_when_overlay_repo_not_found(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        _write_teatree_toml(
            tmp_path / ".teatree.toml",
            f'[teatree]\nworkspace_dir = "{tmp_path}"\n',
        )
        ConfigSetting.objects.set_value("contribute", value=True)
        # No ``my-overlay`` directory under workspace_dir.
        monkeypatch.setattr(
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

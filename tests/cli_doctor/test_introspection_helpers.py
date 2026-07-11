"""``IntrospectionHelpers`` — editable-info parsing + package-info printing.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

import json
from unittest.mock import MagicMock, patch

import teatree.cli.doctor.app as teatree_cli_doctor
from teatree.cli.doctor import IntrospectionHelpers


class TestEditableInfo:
    """``editable_info`` parses ``direct_url.json`` from an installed dist.

    ``importlib.metadata.distribution`` is the external boundary — it walks
    the real site-packages. We mock it rather than install fixture packages.
    """

    def test_returns_false_when_not_installed(self):
        with patch.object(
            teatree_cli_doctor,
            "distribution",
            side_effect=teatree_cli_doctor.PackageNotFoundError("x"),
        ):
            assert IntrospectionHelpers.editable_info("nonexistent") == (False, "")

    def test_returns_false_when_no_direct_url(self):
        dist = MagicMock()
        dist.read_text.return_value = None
        with patch.object(teatree_cli_doctor, "distribution", return_value=dist):
            assert IntrospectionHelpers.editable_info("some-pkg") == (False, "")

    def test_returns_editable_metadata_from_direct_url(self):
        dist = MagicMock()
        dist.read_text.return_value = json.dumps(
            {"dir_info": {"editable": True}, "url": "file:///home/user/project"},
        )
        with patch.object(teatree_cli_doctor, "distribution", return_value=dist):
            editable, url = IntrospectionHelpers.editable_info("some-pkg")

        assert editable is True
        assert url == "file:///home/user/project"

    def test_returns_false_on_invalid_direct_url_json(self):
        dist = MagicMock()
        dist.read_text.return_value = "not json"
        with patch.object(teatree_cli_doctor, "distribution", return_value=dist):
            assert IntrospectionHelpers.editable_info("some-pkg") == (False, "")


class TestPrintPackageInfo:
    """``print_package_info`` resolves ``import_name`` via ``importlib.import_module``."""

    def test_prints_source_dir_for_installed_package(self, capsys):
        with (
            patch("importlib.import_module") as mock_import,
            patch.object(IntrospectionHelpers, "editable_info", return_value=(False, "")),
        ):
            mod = MagicMock()
            mod.__file__ = "/usr/lib/python/teatree/__init__.py"
            mock_import.return_value = mod
            IntrospectionHelpers.print_package_info("teatree", "teatree")

        out = capsys.readouterr().out
        assert "/usr/lib/python/teatree" in out
        assert "installed" in out

    def test_handles_import_error(self, capsys):
        with patch("importlib.import_module", side_effect=ImportError("nope")):
            IntrospectionHelpers.print_package_info("teatree", "teatree")

        assert "not installed" in capsys.readouterr().out

    def test_prints_editable_url_when_available(self, capsys):
        with (
            patch("importlib.import_module") as mock_import,
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "file:///src")),
        ):
            mod = MagicMock()
            mod.__file__ = "/src/teatree/__init__.py"
            mock_import.return_value = mod
            IntrospectionHelpers.print_package_info("teatree", "teatree")

        out = capsys.readouterr().out
        assert "editable" in out
        assert "file:///src" in out

    def test_omits_url_when_editable_but_none(self, capsys):
        with (
            patch("importlib.import_module") as mock_import,
            patch.object(IntrospectionHelpers, "editable_info", return_value=(True, "")),
        ):
            mod = MagicMock()
            mod.__file__ = "/src/teatree/__init__.py"
            mock_import.return_value = mod
            IntrospectionHelpers.print_package_info("teatree", "teatree")

        out = capsys.readouterr().out
        assert "editable" in out
        assert "file://" not in out

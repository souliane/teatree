"""Tests for teatree.core.provisioners — generic provisioning utilities."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase

import teatree.core.provisioners as provisioners_mod
from teatree.core.provisioners import (
    apply_symlinks,
    inject_settings,
    start_services,
)


class TestApplySymlinks(TestCase):
    def test_creates_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            source.write_text("content")

            created = apply_symlinks(
                [{"path": "link.txt", "source": str(source), "mode": "symlink"}],
                tmp,
            )

            link = Path(tmp) / "link.txt"
            assert str(link) in created
            assert link.is_symlink()
            assert link.read_text() == "content"

    def test_symlink_replaces_existing_symlink(self) -> None:
        """When the target already exists as a symlink, it is replaced."""
        with tempfile.TemporaryDirectory() as tmp:
            old_source = Path(tmp) / "old.txt"
            old_source.write_text("old")
            new_source = Path(tmp) / "new.txt"
            new_source.write_text("new")

            target = Path(tmp) / "link.txt"
            target.symlink_to(old_source)
            assert target.read_text() == "old"

            apply_symlinks(
                [{"path": "link.txt", "source": str(new_source), "mode": "symlink"}],
                tmp,
            )

            assert target.is_symlink()
            assert target.read_text() == "new"

    def test_symlink_replaces_existing_file(self) -> None:
        """When the target already exists as a regular file, it is replaced."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            source.write_text("source-content")

            target = Path(tmp) / "link.txt"
            target.write_text("stale-content")
            assert not target.is_symlink()

            apply_symlinks(
                [{"path": "link.txt", "source": str(source), "mode": "symlink"}],
                tmp,
            )

            assert target.is_symlink()
            assert target.read_text() == "source-content"

    def test_creates_copy_of_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            source.write_text("content")

            created = apply_symlinks(
                [{"path": "copy.txt", "source": str(source), "mode": "copy"}],
                tmp,
            )

            copy = Path(tmp) / "copy.txt"
            assert str(copy) in created
            assert not copy.is_symlink()
            assert copy.read_text() == "content"

    def test_creates_copy_of_directory(self) -> None:
        """Copying a directory source uses copytree."""
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "src_dir"
            source_dir.mkdir()
            (source_dir / "a.txt").write_text("alpha")
            (source_dir / "b.txt").write_text("beta")

            created = apply_symlinks(
                [{"path": "dest_dir", "source": str(source_dir), "mode": "copy"}],
                tmp,
            )

            dest = Path(tmp) / "dest_dir"
            assert str(dest) in created
            assert dest.is_dir()
            assert (dest / "a.txt").read_text() == "alpha"
            assert (dest / "b.txt").read_text() == "beta"

    def test_copy_directory_replaces_existing(self) -> None:
        """When copying a directory and the target already exists, the old tree is removed first."""
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "src_dir"
            source_dir.mkdir()
            (source_dir / "new.txt").write_text("new")

            target_dir = Path(tmp) / "dest_dir"
            target_dir.mkdir()
            (target_dir / "old.txt").write_text("old")

            apply_symlinks(
                [{"path": "dest_dir", "source": str(source_dir), "mode": "copy"}],
                tmp,
            )

            assert (target_dir / "new.txt").read_text() == "new"
            assert not (target_dir / "old.txt").exists()

    def test_copy_and_patch_uses_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            source.write_text("content")

            created = apply_symlinks(
                [{"path": "patched.txt", "source": str(source), "mode": "copy-and-patch"}],
                tmp,
            )

            assert str(Path(tmp) / "patched.txt") in created

    def test_unknown_mode_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            source.write_text("content")

            with patch.object(provisioners_mod.logger, "warning") as mock_warn:
                created = apply_symlinks(
                    [{"path": "bad.txt", "source": str(source), "mode": "unknown"}],
                    tmp,
                )

            assert created == []
            assert mock_warn.call_count == 1

    def test_skips_empty_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assert apply_symlinks([{}], tmp) == []


class TestStartServices(TestCase):
    def test_runs_explicit_start_command(self) -> None:
        """A service with start_command runs that command directly."""
        mock_proc = MagicMock(returncode=0, stderr="", stdout="")
        specs = {"db": {"start_command": ["echo", "starting"]}}

        with patch("teatree.utils.run.subprocess.run", return_value=mock_proc) as mock_run:
            results = start_services(specs)

        assert results == {"db": True}
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["echo", "starting"]

    def test_builds_docker_compose_command_from_compose_file(self) -> None:
        """When no start_command but compose_file is present, docker compose up is built."""
        mock_proc = MagicMock(returncode=0, stderr="", stdout="")
        specs = {"redis": {"compose_file": "/path/to/compose.yml", "service": "redis-svc"}}

        with patch("teatree.utils.run.subprocess.run", return_value=mock_proc) as mock_run:
            results = start_services(specs)

        assert results == {"redis": True}
        cmd = mock_run.call_args[0][0]
        assert cmd == ["docker", "compose", "-f", "/path/to/compose.yml", "up", "-d", "redis-svc"]

    def test_compose_file_defaults_service_to_name(self) -> None:
        """When service key is absent, the spec dict key is used as the service name."""
        mock_proc = MagicMock(returncode=0, stderr="", stdout="")
        specs = {"postgres": {"compose_file": "/path/compose.yml"}}

        with patch("teatree.utils.run.subprocess.run", return_value=mock_proc) as mock_run:
            results = start_services(specs)

        assert results == {"postgres": True}
        cmd = mock_run.call_args[0][0]
        assert cmd[-1] == "postgres"

    def test_warns_when_no_command_or_compose_file(self) -> None:
        """A service with neither start_command nor compose_file logs a warning and fails."""
        specs = {"mystery": {}}

        with patch.object(provisioners_mod.logger, "warning") as mock_warn:
            results = start_services(specs)

        assert results == {"mystery": False}
        assert mock_warn.call_count == 1
        assert "mystery" in str(mock_warn.call_args)

    def test_reports_failure_on_nonzero_returncode(self) -> None:
        mock_proc = MagicMock(returncode=1, stderr="error details", stdout="")
        specs = {"web": {"start_command": ["start-web"]}}

        with (
            patch("teatree.utils.run.subprocess.run", return_value=mock_proc),
            patch.object(provisioners_mod.logger, "warning") as mock_warn,
        ):
            results = start_services(specs)

        assert results == {"web": False}
        assert mock_warn.call_count == 1

    def test_reports_failure_on_file_not_found(self) -> None:
        """FileNotFoundError (missing binary) is caught and reported as failure."""
        specs = {"broken": {"start_command": ["nonexistent-binary"]}}

        with (
            patch(
                "teatree.utils.run.subprocess.run",
                side_effect=FileNotFoundError("not found"),
            ),
            patch.object(provisioners_mod.logger, "warning") as mock_warn,
        ):
            results = start_services(specs)

        assert results == {"broken": False}
        assert mock_warn.call_count == 1

    def test_merges_custom_env(self) -> None:
        """Custom env dict is merged with os.environ."""
        mock_proc = MagicMock(returncode=0, stderr="", stdout="")
        specs = {"svc": {"start_command": ["echo"]}}

        with patch("teatree.utils.run.subprocess.run", return_value=mock_proc) as mock_run:
            start_services(specs, env={"CUSTOM_VAR": "custom_val"})

        call_env = mock_run.call_args[1]["env"]
        assert call_env["CUSTOM_VAR"] == "custom_val"

    def test_handles_multiple_services(self) -> None:
        """Multiple services are started independently; mixed results are returned."""
        ok_proc = MagicMock(returncode=0, stderr="", stdout="")
        fail_proc = MagicMock(returncode=1, stderr="boom", stdout="")

        specs = {
            "good": {"start_command": ["start-good"]},
            "bad": {"start_command": ["start-bad"]},
        }

        def side_effect(cmd, **_kwargs):
            if cmd == ["start-good"]:
                return ok_proc
            return fail_proc

        with patch("teatree.utils.run.subprocess.run", side_effect=side_effect):
            results = start_services(specs)

        assert results["good"] is True
        assert results["bad"] is False


class TestInjectSettings(TestCase):
    def test_creates_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.env"
            inject_settings(target, {"KEY": "value"})
            assert target.read_text().strip() == "KEY=value"

    def test_updates_existing_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.env"
            target.write_text("KEY=old\nOTHER=keep\n")

            inject_settings(target, {"KEY": "new"})

            lines = target.read_text().strip().splitlines()
            assert "KEY=new" in lines
            assert "OTHER=keep" in lines

    def test_updates_key_in_place_preserving_order(self) -> None:
        """Existing keys are updated at their original line position."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.env"
            target.write_text("FIRST=1\nSECOND=2\nTHIRD=3\n")

            inject_settings(target, {"SECOND": "updated"})

            lines = target.read_text().strip().splitlines()
            assert lines[0] == "FIRST=1"
            assert lines[1] == "SECOND=updated"
            assert lines[2] == "THIRD=3"

    def test_adds_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.env"
            inject_settings(target, {"DB_HOST": "localhost"}, header="Database")

            content = target.read_text()
            assert "# Database" in content
            assert "DB_HOST=localhost" in content

"""Tests for teatree.core.provisioners — generic provisioning utilities."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import teatree.core.provisioners as provisioners_mod
from teatree.core.provisioners import apply_symlinks, inject_settings


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

    def test_creates_copy(self) -> None:
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

    def test_adds_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.env"
            inject_settings(target, {"DB_HOST": "localhost"}, header="Database")

            content = target.read_text()
            assert "# Database" in content
            assert "DB_HOST=localhost" in content

"""``check_for_updates`` cache + network behavior and ``_write_update_cache``.

Split verbatim from the former monolithic ``tests/test_config.py``
(souliane/teatree#443). Covers the disabled-check early return, the
fresh/empty/corrupt JSON cache paths, the ``gh`` CLI outcomes (empty
tag, timeout, missing binary, newer vs same version) and the cache
writer.

Integration-first per the Test-Writing Doctrine: real TOML fixtures
under ``tmp_path``. Mocks are reserved for the ``gh`` CLI call (network)
and ``importlib.metadata.version``.
"""

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.config import check_for_updates
from teatree.update_check import _write_update_cache

from ._shared import _write_toml


def _write_check_updates_toml(config_file: Path, *, enabled: bool) -> None:
    _write_toml(config_file, f"[teatree]\ncheck_updates = {'true' if enabled else 'false'}\n")


class TestCheckForUpdates:
    def test_returns_none_when_updates_disabled(self, config_file: Path) -> None:
        """Early return None when check_updates=false and force=False."""
        _write_check_updates_toml(config_file, enabled=False)

        assert check_for_updates(force=False) is None

    def test_disabled_check_honoured_pre_django_without_network(self, config_file: Path) -> None:
        """A TOML ``check_updates=false`` is honoured with no Django/DB and no network.

        config-unify PR5 audit: ``check_for_updates`` resolves ``check_updates``
        from ``load_config().user`` (the TOML file tier), so the opt-out holds on
        the pre-Django CLI paths that are its only readers. This guard is
        anti-vacuous against a DB-home move of ``check_updates``: dropping the
        loader field-build would resolve ``check_updates`` to its ``True`` default
        here, skip the early return, and reach the network call — turning this red.
        """
        _write_check_updates_toml(config_file, enabled=False)

        with patch("teatree.update_check.run_allowed_to_fail") as network:
            assert check_for_updates(force=False) is None
        network.assert_not_called()

    def test_cached_result_returned_when_fresh(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_file: Path,
    ) -> None:
        """Return cached message when within TTL."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cache_path = data_dir / "update-check.json"
        cache_path.write_text(
            json.dumps({"ts": time.time(), "message": "teatree v9.9 available"}),
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.update_check.DATA_DIR", data_dir)

        assert check_for_updates(force=False) == "teatree v9.9 available"

    def test_cached_empty_message_returns_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_file: Path,
    ) -> None:
        """Cached empty message means up-to-date => None."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cache_path = data_dir / "update-check.json"
        cache_path.write_text(
            json.dumps({"ts": time.time(), "message": ""}),
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.update_check.DATA_DIR", data_dir)

        assert check_for_updates(force=False) is None

    def test_cached_corrupt_json_falls_through(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_file: Path,
    ) -> None:
        """Corrupt cache JSON is silently ignored, proceeds to network check."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        cache_path = data_dir / "update-check.json"
        cache_path.write_text("NOT VALID JSON {{{", encoding="utf-8")
        monkeypatch.setattr("teatree.update_check.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="v1.0.0\n")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            # Falls through corrupt cache, hits network, finds same version
            assert check_for_updates(force=False) is None

    def test_empty_tag_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
        """When gh returns empty tag, returns None."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.update_check.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="\n")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            assert check_for_updates(force=True) is None

    def test_subprocess_timeout_returns_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_file: Path,
    ) -> None:
        """TimeoutExpired from gh CLI returns None."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.update_check.DATA_DIR", data_dir)

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 10)):
            assert check_for_updates(force=True) is None

    def test_file_not_found_returns_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_file: Path,
    ) -> None:
        """FileNotFoundError (gh not installed) returns None."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.update_check.DATA_DIR", data_dir)

        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert check_for_updates(force=True) is None

    def test_newer_version_returns_upgrade_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_file: Path,
    ) -> None:
        """When latest != current, returns upgrade message."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.update_check.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="v2.0.0\n")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            result = check_for_updates(force=True)

        assert result is not None
        assert "v2.0.0" in result
        assert "1.0.0" in result
        assert "uv pip install --upgrade teatree" in result

        cache_path = data_dir / "update-check.json"
        assert cache_path.is_file()
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert "v2.0.0" in cached["message"]

    def test_same_version_returns_none_and_caches(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_file: Path,
    ) -> None:
        """When latest == current, returns None and caches empty."""
        _write_check_updates_toml(config_file, enabled=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr("teatree.update_check.DATA_DIR", data_dir)

        mock_result = MagicMock(stdout="v1.0.0\n")
        with (
            patch("subprocess.run", return_value=mock_result),
            patch("importlib.metadata.version", return_value="1.0.0"),
        ):
            result = check_for_updates(force=True)

        assert result is None
        cache_path = data_dir / "update-check.json"
        assert cache_path.is_file()
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert cached["message"] == ""


# ── _write_update_cache ──────────────────────────────────────────────


class TestWriteUpdateCache:
    def test_creates_parent_dirs_and_writes_json(self, tmp_path: Path) -> None:
        """Creates parent dirs and writes valid JSON cache."""
        cache_path = tmp_path / "nested" / "dir" / "update-check.json"
        _write_update_cache(cache_path, "test message")

        assert cache_path.is_file()
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert data["message"] == "test message"
        assert "ts" in data
        assert isinstance(data["ts"], float)

"""Snapshot-warmer mechanical handler: refresh a stale reference DB (souliane/teatree#2949)."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.loop.dispatch_tables import MECHANICAL_BY_KIND
from teatree.loop.mechanical import HANDLERS, refresh_snapshot
from teatree.utils.django_db import DjangoDbImportConfig


def _cfg(tmp_path: Path) -> DjangoDbImportConfig:
    return DjangoDbImportConfig(
        ref_db_name="development-acme",
        ticket_db_name="wt_development-acme",
        main_repo_path=str(tmp_path),
        dump_dir=str(tmp_path / ".data"),
        dump_glob="*.pgsql",
        ci_dump_glob="*.sql.gz",
    )


class TestRefreshSnapshotHandler:
    def test_no_config_in_payload_is_a_no_op(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            refresh_snapshot({})
        assert "no config" in caplog.text

    def test_successful_refresh_logs_info(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        cfg = _cfg(tmp_path)
        with (
            patch("teatree.utils.django_db_snapshot_warmer.refresh_reference_snapshot", return_value=True),
            caplog.at_level(logging.INFO),
        ):
            refresh_snapshot({"config": cfg})
        assert "development-acme" in caplog.text

    def test_failed_refresh_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        cfg = _cfg(tmp_path)
        with (
            patch("teatree.utils.django_db_snapshot_warmer.refresh_reference_snapshot", return_value=False),
            caplog.at_level(logging.WARNING),
        ):
            refresh_snapshot({"config": cfg})
        assert "did not succeed" in caplog.text

    def test_exception_is_swallowed(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        with patch(
            "teatree.utils.django_db_snapshot_warmer.refresh_reference_snapshot", side_effect=RuntimeError("boom")
        ):
            refresh_snapshot({"config": cfg})  # must not raise


class TestSnapshotWarmerWiring:
    def test_dispatch_kind_routes_to_mechanical_refresh_snapshot(self) -> None:
        assert MECHANICAL_BY_KIND["snapshot_warmer.refresh_needed"] == ("mechanical", "refresh_snapshot")

    def test_handler_registry_maps_the_zone_to_refresh_snapshot(self) -> None:
        assert HANDLERS["refresh_snapshot"] is refresh_snapshot

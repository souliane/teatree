"""Snapshot-warmer scanner: emits a signal for each stale reference DB (souliane/teatree#2949)."""

from pathlib import Path
from unittest.mock import patch

from teatree.loop.scanners.snapshot_warmer import SnapshotWarmerScanner
from teatree.utils.django_db import DjangoDbImportConfig


def _cfg(ref_db_name: str, tmp_path: Path) -> DjangoDbImportConfig:
    return DjangoDbImportConfig(
        ref_db_name=ref_db_name,
        ticket_db_name=f"wt_{ref_db_name}",
        main_repo_path=str(tmp_path),
        dump_dir=str(tmp_path / ".data"),
        dump_glob="*.pgsql",
        ci_dump_glob="*.sql.gz",
    )


class TestSnapshotWarmerScanner:
    def test_no_configs_emits_nothing(self) -> None:
        scanner = SnapshotWarmerScanner(configs=[])
        assert scanner.scan() == []

    def test_stale_config_emits_a_signal(self, tmp_path: Path) -> None:
        cfg = _cfg("development-acme", tmp_path)
        with patch("teatree.loop.scanners.snapshot_warmer.snapshot_is_stale", return_value=True):
            signals = SnapshotWarmerScanner(configs=[cfg]).scan()
        assert len(signals) == 1
        assert signals[0].kind == "snapshot_warmer.refresh_needed"
        assert "development-acme" in signals[0].summary
        assert signals[0].payload["config"] is cfg

    def test_fresh_config_emits_nothing(self, tmp_path: Path) -> None:
        cfg = _cfg("development-acme", tmp_path)
        with patch("teatree.loop.scanners.snapshot_warmer.snapshot_is_stale", return_value=False):
            signals = SnapshotWarmerScanner(configs=[cfg]).scan()
        assert signals == []

    def test_multiple_configs_only_stale_ones_emit(self, tmp_path: Path) -> None:
        fresh = _cfg("development-alpha", tmp_path)
        stale = _cfg("development-beta", tmp_path)

        def fake_stale(cfg: DjangoDbImportConfig, **_kwargs: object) -> bool:
            return cfg.ref_db_name == "development-beta"

        with patch("teatree.loop.scanners.snapshot_warmer.snapshot_is_stale", side_effect=fake_stale):
            signals = SnapshotWarmerScanner(configs=[fresh, stale]).scan()
        assert len(signals) == 1
        assert signals[0].payload["config"] is stale

    def test_a_broken_probe_is_swallowed_and_others_still_scan(self, tmp_path: Path) -> None:
        broken = _cfg("development-broken", tmp_path)
        stale = _cfg("development-beta", tmp_path)

        def fake_stale(cfg: DjangoDbImportConfig, **_kwargs: object) -> bool:
            if cfg.ref_db_name == "development-broken":
                msg = "boom"
                raise RuntimeError(msg)
            return True

        with patch("teatree.loop.scanners.snapshot_warmer.snapshot_is_stale", side_effect=fake_stale):
            signals = SnapshotWarmerScanner(configs=[broken, stale]).scan()
        assert len(signals) == 1
        assert signals[0].payload["config"] is stale

    def test_max_age_days_is_passed_through(self, tmp_path: Path) -> None:
        cfg = _cfg("development-acme", tmp_path)
        with patch("teatree.loop.scanners.snapshot_warmer.snapshot_is_stale", return_value=False) as mock_stale:
            SnapshotWarmerScanner(configs=[cfg], max_age_days=3).scan()
        mock_stale.assert_called_once_with(cfg, max_age_days=3)

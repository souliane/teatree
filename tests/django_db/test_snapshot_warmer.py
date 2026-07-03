"""Tests for the snapshot-warmer entry points (souliane/teatree#2949).

``snapshot_age_days`` / ``snapshot_is_stale`` derive freshness purely from
the DSLR snapshot's own ``YYYYMMDD_<tenant>`` name — no separate persisted
marker. ``refresh_reference_snapshot`` reuses ``DjangoDbImporter``'s private
restore/migrate/snapshot machinery to bring a reference DB current
out-of-band.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from teatree.utils import django_db_dslr as dslr_mod
from teatree.utils.django_db import DjangoDbImporter, _MigrateResult
from teatree.utils.django_db_snapshot_warmer import refresh_reference_snapshot, snapshot_age_days, snapshot_is_stale

from ._shared import _make_cfg


class TestSnapshotAgeDays:
    def test_none_when_no_dslr_tool(self, tmp_path: Path) -> None:
        cfg = _make_cfg(tmp_path, snapshot_tool="")
        assert snapshot_age_days(cfg) is None

    def test_none_when_no_snapshots_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *_a, **_k: [])
        cfg = _make_cfg(tmp_path)
        assert snapshot_age_days(cfg) is None

    def test_zero_for_a_snapshot_taken_today(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        today = datetime.now(tz=UTC).strftime("%Y%m%d")
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *_a, **_k: [f"{today}_development-acme"])
        cfg = _make_cfg(tmp_path)
        assert snapshot_age_days(cfg) == 0

    def test_computes_age_from_the_embedded_date(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *_a, **_k: ["20260101_development-acme"])
        cfg = _make_cfg(tmp_path)
        assert snapshot_age_days(cfg, now=datetime(2026, 1, 6, tzinfo=UTC)) == 5

    def test_none_when_newest_name_is_unparseable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *_a, **_k: ["not-a-date_development-acme"])
        cfg = _make_cfg(tmp_path)
        assert snapshot_age_days(cfg) is None


class TestSnapshotIsStale:
    def test_stale_when_no_snapshot(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *_a, **_k: [])
        cfg = _make_cfg(tmp_path)
        assert snapshot_is_stale(cfg) is True

    def test_fresh_when_within_max_age(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *_a, **_k: ["20260101_development-acme"])
        cfg = _make_cfg(tmp_path)
        assert snapshot_is_stale(cfg, max_age_days=2, now=datetime(2026, 1, 2, tzinfo=UTC)) is False

    def test_stale_when_older_than_max_age(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr(dslr_mod, "find_dslr_snapshots", lambda *_a, **_k: ["20260101_development-acme"])
        cfg = _make_cfg(tmp_path)
        assert snapshot_is_stale(cfg, max_age_days=1, now=datetime(2026, 1, 5, tzinfo=UTC)) is True


class TestRefreshReferenceSnapshot:
    def test_no_dslr_tool_returns_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: [])
        cfg = _make_cfg(tmp_path, snapshot_tool="")
        assert refresh_reference_snapshot(cfg) is False

    def test_restores_migrates_and_snapshots_on_applied_migrations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr("teatree.utils.django_db_snapshot_warmer._ensure_ref_db", lambda *_a, **_k: None)
        monkeypatch.setattr(DjangoDbImporter, "_resolve_dslr_snapshots", lambda self: ["20260101_development-acme"])
        monkeypatch.setattr(dslr_mod, "restore_ref_from_dslr", lambda *_a, **_k: (True, False, ""))
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.APPLIED)
        snapshotted: list[bool] = []
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: snapshotted.append(True))
        cfg = _make_cfg(tmp_path)

        assert refresh_reference_snapshot(cfg) is True
        assert snapshotted == [True]

    def test_no_new_snapshot_when_already_migrated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr("teatree.utils.django_db_snapshot_warmer._ensure_ref_db", lambda *_a, **_k: None)
        monkeypatch.setattr(DjangoDbImporter, "_resolve_dslr_snapshots", lambda self: ["20260101_development-acme"])
        monkeypatch.setattr(dslr_mod, "restore_ref_from_dslr", lambda *_a, **_k: (True, False, ""))
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.ALREADY_MIGRATED)
        snapshotted: list[bool] = []
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: snapshotted.append(True))
        cfg = _make_cfg(tmp_path)

        assert refresh_reference_snapshot(cfg) is True
        assert snapshotted == []

    def test_restore_failure_returns_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr("teatree.utils.django_db_snapshot_warmer._ensure_ref_db", lambda *_a, **_k: None)
        monkeypatch.setattr(DjangoDbImporter, "_resolve_dslr_snapshots", lambda self: ["20260101_development-acme"])
        monkeypatch.setattr(dslr_mod, "restore_ref_from_dslr", lambda *_a, **_k: (False, False, "boom"))
        cfg = _make_cfg(tmp_path)

        assert refresh_reference_snapshot(cfg) is False

    def test_migrate_failure_returns_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr("teatree.utils.django_db_snapshot_warmer._ensure_ref_db", lambda *_a, **_k: None)
        monkeypatch.setattr(DjangoDbImporter, "_resolve_dslr_snapshots", lambda self: [])
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.FAILED)
        cfg = _make_cfg(tmp_path)

        assert refresh_reference_snapshot(cfg) is False

    def test_no_existing_snapshot_still_migrates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No prior snapshot at all — migrate from a freshly-created empty ref DB."""
        monkeypatch.setattr(dslr_mod, "find_dslr_cmd", lambda *_a, **_k: ["/usr/bin/dslr"])
        monkeypatch.setattr("teatree.utils.django_db_snapshot_warmer._ensure_ref_db", lambda *_a, **_k: None)
        monkeypatch.setattr(DjangoDbImporter, "_resolve_dslr_snapshots", lambda self: [])
        monkeypatch.setattr(DjangoDbImporter, "_migrate_reference_db", lambda self: _MigrateResult.APPLIED)
        snapshotted: list[bool] = []
        monkeypatch.setattr(DjangoDbImporter, "_take_dslr_snapshot", lambda self: snapshotted.append(True))
        cfg = _make_cfg(tmp_path)

        assert refresh_reference_snapshot(cfg) is True
        assert snapshotted == [True]

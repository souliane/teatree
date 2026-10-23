"""Health records keep host and container observations separate and time bound."""

import json
from pathlib import Path

import pytest

from teatree.hooks import orm_health


def test_record_round_trip_and_venue_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    monkeypatch.setattr(orm_health, "hook_venue", lambda: "host")
    host_path = orm_health.health_path()
    orm_health.record_orm_health("unreachable", "/missing/db", "missing")
    monkeypatch.setattr(orm_health, "hook_venue", lambda: "container")
    container_path = orm_health.health_path()
    orm_health.record_orm_health("ok", "/working/db")

    assert host_path != container_path
    records = orm_health.read_orm_health()
    assert {(r["venue"], r["status"]) for r in records} == {("host", "unreachable"), ("container", "ok")}
    assert next(r for r in records if r["venue"] == "host")["db_path"] == "/missing/db"


def test_expired_and_corrupt_records_are_not_healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    orm_health.record_orm_health("ok", "/working/db")
    path = orm_health.health_path()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert orm_health.read_orm_health(now=record["at"] + orm_health.MARKER_TTL_SECONDS + 1) == []

    path.write_text("{broken", encoding="utf-8")
    records = orm_health.read_orm_health()
    assert len(records) == 1
    assert records[0]["status"] == "error"


def test_failure_replaces_recent_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    orm_health.record_orm_health("ok", "/working/db")
    orm_health.record_orm_health("unreachable", "/missing/db", "gone")

    assert orm_health.read_orm_health()[0]["status"] == "unreachable"


def test_healthy_probe_replaces_corrupt_recent_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    path = orm_health.health_path()
    path.write_text("{broken", encoding="utf-8")

    orm_health.record_orm_health("ok", "/working/db")

    assert orm_health.read_orm_health()[0]["status"] == "ok"


def test_record_reports_whether_the_state_was_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path / "state")
    assert orm_health.record_orm_health("unreachable", "/missing/db", "gone") is True
    assert orm_health.record_orm_health("ok", "/working/db") is True
    assert orm_health.record_orm_health("ok", "/working/db") is True

    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: blocker / "state")
    assert orm_health.record_orm_health("unreachable", "/missing/db", "gone") is False

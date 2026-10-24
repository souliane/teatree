"""The doctor reports the hook interpreter's measured ORM reachability."""

from pathlib import Path

import pytest

from hooks.scripts import django_bootstrap
from teatree.cli.doctor import checks_cold_hooks
from teatree.cli.doctor.checks_cold_hooks import _check_hook_orm_reachable
from teatree.hooks import orm_health


def test_absent_record_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    monkeypatch.setattr(checks_cold_hooks, "_run_hook_probe", lambda *_args: None)

    assert _check_hook_orm_reachable() is False
    output = capsys.readouterr().out
    assert "WARN" in output
    assert "UNVERIFIED" in output


def test_recent_ok_record_cannot_replace_a_live_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    monkeypatch.setattr(checks_cold_hooks, "_run_hook_probe", lambda *_args: None)
    orm_health.record_orm_health("ok", "/working/control.sqlite3")

    assert _check_hook_orm_reachable() is False
    assert "UNVERIFIED" in capsys.readouterr().out


def test_unreachable_host_record_is_not_masked_by_container_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    monkeypatch.setattr(checks_cold_hooks, "_run_hook_probe", lambda *_args: None)
    monkeypatch.setattr(orm_health, "hook_venue", lambda: "host")
    orm_health.record_orm_health("unreachable", "/missing/control.sqlite3", "FileNotFoundError")
    monkeypatch.setattr(orm_health, "hook_venue", lambda: "container")
    orm_health.record_orm_health("ok", "/container/control.sqlite3")

    assert _check_hook_orm_reachable() is False
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert "/missing/control.sqlite3" in output
    assert orm_health.sys.executable in output


def test_missing_django_on_host_makes_container_doctor_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    monkeypatch.setattr(orm_health, "hook_venue", lambda: "host")

    def missing_django() -> None:
        raise ModuleNotFoundError

    monkeypatch.setattr(django_bootstrap, "_django_setup", missing_django)
    monkeypatch.setattr(django_bootstrap, "_EMITTED_NOTES", set())
    assert django_bootstrap.bootstrap_teatree_django() is False

    monkeypatch.setattr(orm_health, "hook_venue", lambda: "container")
    monkeypatch.setattr(
        checks_cold_hooks,
        "_run_hook_probe",
        lambda *_args: {
            "status": "ok",
            "interpreter": "/container/python",
            "db_path": "/container/control.sqlite3",
            "venue": "container",
        },
    )

    assert _check_hook_orm_reachable() is False
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert "(host)" in output
    assert "ModuleNotFoundError" in output


def test_ok_record_passes_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        checks_cold_hooks,
        "_run_hook_probe",
        lambda *_args: {"status": "ok", "interpreter": "/hook/python", "db_path": "/working/control.sqlite3"},
    )
    orm_health.record_orm_health("ok", "/working/control.sqlite3")

    assert _check_hook_orm_reachable() is True
    assert capsys.readouterr().out == ""


def test_live_hook_interpreter_probe_failure_is_a_doctor_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        checks_cold_hooks,
        "_run_hook_probe",
        lambda *_args: {
            "status": "unreachable",
            "interpreter": "/hook/python",
            "db_path": "/missing/db",
            "cause": "not open",
        },
    )

    assert _check_hook_orm_reachable() is False
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert "/hook/python" in output
    assert "/missing/db" in output


def test_live_hook_interpreter_probe_passes_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        checks_cold_hooks,
        "_run_hook_probe",
        lambda *_args: {"status": "ok", "interpreter": "/hook/python", "db_path": "/working/db"},
    )

    assert _check_hook_orm_reachable() is True
    assert capsys.readouterr().out == ""

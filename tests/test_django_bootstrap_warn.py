# test-path: cross-cutting — tests hooks/scripts/django_bootstrap.py, which has no src/teatree/ mirror.
"""A failed Django bootstrap must be AUDIBLE — a mute skip reads as a clean pass.

``run-hook.sh`` deliberately falls back to a version-floor-only interpreter with
no Django and prints nothing. On such a host every ORM-backed gate degrades to a
silent allow, and an operator inspecting the session sees a clean run and
concludes the gates passed. The bootstrap therefore names the missing capability
once per process, and the gates that degrade on it say SKIPPED rather than
nothing.
"""

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr

import pytest
from django.test import TestCase

from hooks.scripts import django_bootstrap
from teatree.hooks import orm_health


@pytest.fixture(autouse=True)
def _reset_warn_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(django_bootstrap, "_MISSING_CAPABILITY_WARNED", False)
    monkeypatch.setattr(orm_health, "record_orm_health", lambda *_args: None)


_NO_DJANGO = ModuleNotFoundError("No module named 'django'")


def _break_django_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``django.setup()`` fail the way a Django-less interpreter does."""

    def _boom() -> None:
        raise _NO_DJANGO

    monkeypatch.setattr(django_bootstrap, "_django_setup", _boom)


class TestFailureIsAudible:
    def test_failed_bootstrap_names_the_missing_capability(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _break_django_setup(monkeypatch)

        assert django_bootstrap.bootstrap_teatree_django() is False

        err = capsys.readouterr().err
        assert "cannot import Django" in err
        assert "No module named 'django'" in err

    def test_missing_django_records_failed_host_orm_health(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _break_django_setup(monkeypatch)
        recorded: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            orm_health,
            "record_orm_health",
            lambda status, db_path, cause: recorded.append((status, db_path, cause)),
        )

        assert django_bootstrap.bootstrap_teatree_django() is False
        assert recorded == [("error", "unresolved", "ModuleNotFoundError: No module named 'django'")]

    def test_failed_health_recording_still_skips_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _break_django_setup(monkeypatch)

        def broken_record(*_args: str) -> None:
            raise RuntimeError

        monkeypatch.setattr(orm_health, "record_orm_health", broken_record)

        assert django_bootstrap.bootstrap_teatree_django() is False
        err = capsys.readouterr().err
        assert "SKIPPED" in err
        assert "health marker" in err

    def test_the_warning_is_emitted_once_per_process(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _break_django_setup(monkeypatch)

        django_bootstrap.bootstrap_teatree_django()
        django_bootstrap.bootstrap_teatree_django()

        assert capsys.readouterr().err.count("cannot import Django") == 1

    def test_unexpected_orm_probe_error_is_reported_as_skipped(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def broken_probe() -> tuple[str, str, BaseException | None]:
            raise RuntimeError

        monkeypatch.setattr(django_bootstrap, "_django_setup", lambda: None)
        monkeypatch.setattr(django_bootstrap, "_probe_orm", broken_probe)
        monkeypatch.setattr(orm_health, "record_orm_health", lambda *_args: None)

        assert django_bootstrap.bootstrap_teatree_django() is False
        assert "SKIPPED" in capsys.readouterr().err


class TestSuccessIsSilent(TestCase):
    def test_successful_bootstrap_says_nothing(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            assert django_bootstrap.bootstrap_teatree_django() is True
        assert stderr.getvalue() == ""


def test_unreachable_orm_db_is_reported_by_the_live_dispatch_hook(tmp_path) -> None:
    data_dir = tmp_path / "hook-data"
    env = dict(
        os.environ,
        T3_DATA_DIR=str(data_dir),
        XDG_DATA_HOME=str(tmp_path / "xdg"),
        BROKEN_DB_PATH=str(tmp_path / "missing" / "db.sqlite3"),
    )
    script = (
        "import json, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'teatree.settings'); "
        "import django; django.setup(); "
        "from django.db import connections; "
        "connections['default'].close(); "
        "connections['default'].settings_dict['NAME'] = os.environ['BROKEN_DB_PATH']; "
        "from hooks.scripts.dispatch_admission_gate import handle_dispatch_admission; "
        "print(json.dumps({'denied': handle_dispatch_admission({'tool_name': 'Agent', "
        "'tool_input': {'prompt': 'work'}})}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=20, env=env
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["denied"] is False
    assert "SKIPPED" in result.stderr
    assert env["BROKEN_DB_PATH"] in result.stderr
    assert sys.executable in result.stderr
    records = list(data_dir.glob("hook-orm-health-*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["status"] == "unreachable"

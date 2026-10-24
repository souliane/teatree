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
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError, ProgrammingError
from django.test import TestCase

from hooks.scripts import django_bootstrap
from teatree.core.models import StandingGoal
from teatree.hooks import orm_health

_RECORD_ORM_HEALTH = orm_health.record_orm_health


@pytest.fixture(autouse=True)
def _hook_orm_probe_reads_ok() -> None:
    """Overrides the conftest default: this module exercises the real probe."""


@pytest.fixture(autouse=True)
def _reset_warn_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(django_bootstrap, "_EMITTED_NOTES", set())
    monkeypatch.setattr(orm_health, "record_orm_health", lambda *_args: True)


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

    @pytest.mark.parametrize(
        "failure", [RuntimeError("no home directory"), ImportError("gone"), PermissionError("read-only")]
    )
    def test_failed_health_recording_still_skips_the_gate(
        self, failure: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _break_django_setup(monkeypatch)

        def broken_record(*_args: str) -> None:
            raise failure

        monkeypatch.setattr(orm_health, "record_orm_health", broken_record)

        assert django_bootstrap.bootstrap_teatree_django() is False
        err = capsys.readouterr().err
        assert "SKIPPED" in err
        assert "health marker" in err
        assert type(failure).__name__ in err

    def test_an_unlisted_health_recording_error_is_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _break_django_setup(monkeypatch)

        def broken_record(*_args: str) -> None:
            raise KeyError

        monkeypatch.setattr(orm_health, "record_orm_health", broken_record)

        with pytest.raises(KeyError):
            django_bootstrap.bootstrap_teatree_django()

    @pytest.mark.parametrize("django_importable", [False, True])
    def test_unwritable_health_record_is_named_on_stderr(
        self,
        *,
        django_importable: bool,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(orm_health, "shared_hook_state_root", lambda: blocker / "state")
        monkeypatch.setattr(orm_health, "record_orm_health", _RECORD_ORM_HEALTH)
        if django_importable:
            monkeypatch.setattr(django_bootstrap, "_django_setup", lambda: None)
            monkeypatch.setattr(
                django_bootstrap, "_probe_orm", lambda: ("unreachable", "/missing/db", FileNotFoundError("/missing/db"))
            )
            monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "teatree.settings")
        else:
            _break_django_setup(monkeypatch)

        assert django_bootstrap.bootstrap_teatree_django() is False

        assert "health marker could not be recorded" in capsys.readouterr().err

    def test_the_warning_is_emitted_once_per_process(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _break_django_setup(monkeypatch)

        django_bootstrap.bootstrap_teatree_django()
        django_bootstrap.bootstrap_teatree_django()

        assert capsys.readouterr().err.count("cannot import Django") == 1

    @pytest.mark.parametrize(
        "failure",
        [
            ImproperlyConfigured("settings are unusable"),
            SyntaxError("broken module"),
            PermissionError("denied"),
            RuntimeError("populate() isn't reentrant"),
        ],
    )
    def test_django_setup_failures_are_reported_and_skip_the_gate(
        self, failure: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def broken_setup() -> None:
            raise failure

        monkeypatch.setattr(django_bootstrap, "_django_setup", broken_setup)

        assert django_bootstrap.bootstrap_teatree_django() is False
        err = capsys.readouterr().err
        assert "SKIPPED" in err
        assert type(failure).__name__ in err

    def test_an_unlisted_django_setup_error_is_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken_setup() -> None:
            raise KeyError

        monkeypatch.setattr(django_bootstrap, "_django_setup", broken_setup)

        with pytest.raises(KeyError):
            django_bootstrap.bootstrap_teatree_django()


class TestOrmProbeFailures(TestCase):
    def _bootstrap_with_failing_query(self, failure: Exception) -> tuple[bool, str, list[tuple[str, str, str]]]:
        recorded: list[tuple[str, str, str]] = []
        stderr = io.StringIO()
        with (
            patch.object(orm_health, "record_orm_health", lambda *args: recorded.append(args)),
            patch.object(StandingGoal.objects, "exists", side_effect=failure),
            redirect_stderr(stderr),
        ):
            bootstrapped = django_bootstrap.bootstrap_teatree_django()
        return bootstrapped, stderr.getvalue(), recorded

    def test_orm_failures_are_reported_as_skipped_with_an_error_record(self) -> None:
        failures = (
            ProgrammingError("no such table: teatree_standinggoal"),
            OperationalError("database is locked"),
            ImproperlyConfigured("settings are unusable"),
            PermissionError("denied"),
        )
        for failure in failures:
            django_bootstrap._EMITTED_NOTES.clear()
            with self.subTest(failure=type(failure).__name__):
                bootstrapped, err, recorded = self._bootstrap_with_failing_query(failure)

                assert bootstrapped is False
                assert "SKIPPED" in err
                assert type(failure).__name__ in err
                assert [status for status, _db, _cause in recorded] == ["error"]

    def test_an_unopenable_database_is_recorded_as_unreachable(self) -> None:
        bootstrapped, err, recorded = self._bootstrap_with_failing_query(
            OperationalError("unable to open database file")
        )

        assert bootstrapped is False
        assert "unreachable" in err
        assert [status for status, _db, _cause in recorded] == ["unreachable"]

    def test_an_unlisted_probe_error_is_not_swallowed(self) -> None:
        with pytest.raises(AttributeError):
            self._bootstrap_with_failing_query(AttributeError("a bug in the probe"))


class TestRegistryOnlyBootstrap:
    def test_an_unreadable_control_db_does_not_skip_a_gate_that_never_reads_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            django_bootstrap, "_probe_orm", lambda: ("unreachable", "/missing/db", FileNotFoundError("/missing/db"))
        )

        assert django_bootstrap.bootstrap_django_registry() is True
        assert capsys.readouterr().err == ""
        assert django_bootstrap.bootstrap_teatree_django() is False

    def test_a_django_less_interpreter_still_skips_it_audibly(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _break_django_setup(monkeypatch)

        assert django_bootstrap.bootstrap_django_registry() is False
        assert "cannot import Django" in capsys.readouterr().err


class TestSuccessIsSilent(TestCase):
    def test_successful_bootstrap_says_nothing(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            assert django_bootstrap.bootstrap_teatree_django() is True
        assert stderr.getvalue() == ""


@pytest.mark.parametrize("broken_db", ["missing/db.sqlite3", "db.sqlite3"], ids=["no-parent-dir", "parent-dir-exists"])
def test_unreachable_orm_db_is_reported_by_the_live_dispatch_hook(tmp_path: Path, broken_db: str) -> None:
    data_dir = tmp_path / "hook-data"
    broken_db_path = tmp_path / broken_db
    env = dict(
        os.environ,
        T3_DATA_DIR=str(data_dir),
        XDG_DATA_HOME=str(tmp_path / "xdg"),
        BROKEN_DB_PATH=str(broken_db_path),
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
    assert not broken_db_path.exists(), "the probe must not create an empty database file"
    assert sys.executable in result.stderr
    records = list(data_dir.glob("hook-orm-health-*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["status"] == "unreachable"

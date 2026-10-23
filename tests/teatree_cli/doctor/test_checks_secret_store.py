import io
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import teatree.cli.doctor.run_checks as doctor_runner
from teatree.cli.doctor.checks_secret_store import check_secret_store
from teatree.core.sealed.store import DbStore
from teatree.utils.master_key import KeyFileSource


class TestSecretStoreDoctor(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = KeyFileSource(Path(temporary.name) / "master.key")
        environment = patch.dict(os.environ, {"T3_MASTER_KEY_SOURCE": self.source.label})
        environment.start()
        self.addCleanup(environment.stop)

    def _doctor(self, *, healthy: bool) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            assert check_secret_store() is healthy
        return output.getvalue()

    def test_no_key_yet_is_a_warning_and_creates_none(self) -> None:
        assert self._doctor(healthy=True).startswith("WARN")
        assert not self.source.path.exists()

    def test_ok_line_carries_the_active_key_id(self) -> None:
        key_id = self.source.create().key_id
        assert (
            self._doctor(healthy=True)
            == f"OK    Sealed secret key {self.source.label}: key_id {key_id}; no sealed rows\n"
        )
        DbStore(self.source).write("alpha/token", "canary")
        report = self._doctor(healthy=True)
        assert report.startswith("OK")
        assert f"key_id {key_id}" in report

    def test_removed_key_fails_naming_the_source(self) -> None:
        DbStore(self.source).write("alpha/token", "canary")
        self.source.path.unlink()
        report = self._doctor(healthy=False)
        assert report.startswith("FAIL")
        assert self.source.label in report

    def test_removed_key_fails_the_doctor_aggregate(self) -> None:
        DbStore(self.source).write("alpha/token", "canary")
        self.source.path.unlink()

        output = io.StringIO()
        with ExitStack() as stack:
            for name in doctor_runner.run_doctor_checks.__code__.co_names:
                if name != "check_secret_store" and callable(getattr(doctor_runner, name, None)):
                    stack.enter_context(patch.object(doctor_runner, name, return_value=True))
            for target in (
                "teatree.core.gates.schema_guard.doctor_check_self_db_migrations",
                "teatree.core.gates.schema_guard.doctor_check_process_code_freshness",
                "teatree.cli.doctor.self_heal.run_self_heal_checks",
                "teatree.cli.update._collect_repos",
                "teatree.core.gates.clone_guard.doctor_check_clone_currency",
            ):
                stack.enter_context(patch(target, return_value=True))
            with redirect_stdout(output):
                result = doctor_runner.run_doctor_checks()

        assert result is False
        assert f"FAIL  Sealed secret key {self.source.label}" in output.getvalue()

    def test_unloadable_key_without_rows_fails(self) -> None:
        self.source.path.write_text("not base64")
        self.source.path.chmod(0o600)
        report = self._doctor(healthy=False)
        assert report.startswith("FAIL")
        assert self.source.label in report


def test_cold_cli_import_is_safe() -> None:
    environment = os.environ.copy()
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", "from teatree.cli import main"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

import io
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

import teatree.cli.doctor.run_checks as doctor_runner
from teatree.cli.doctor.checks_secret_store import check_secret_store
from teatree.core.sealed.store import DbStore
from teatree.utils.master_key import KeyFileSource
from tests._unreadable_file import skip_if_root


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

    def test_no_key_on_read_only_mount_names_host_provisioning(self) -> None:
        with patch(
            "teatree.cli.doctor.checks_secret_store.os.statvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)
        ):
            report = self._doctor(healthy=True)
        assert "provision the key on the host" in report
        assert "first write will create it" not in report

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

    def _volume_key(self) -> KeyFileSource:
        volume = self.source.path.parent / "control"
        volume.mkdir()
        source = KeyFileSource(volume / "master.key")
        environment = patch.dict(
            os.environ,
            {"T3_CONTROL_DB_DIR": str(volume), "TEATREE_ROLE": "worker", "T3_MASTER_KEY_SOURCE": source.label},
        )
        environment.start()
        self.addCleanup(environment.stop)
        return source

    def test_production_key_inside_the_volume_warns_while_nothing_is_stored(self) -> None:
        self._volume_key()
        report = self._doctor(healthy=True)
        assert report.startswith("WARN")
        assert "control-database volume" in report

    def test_production_key_inside_the_volume_fails_once_rows_exist(self) -> None:
        source = self._volume_key()
        DbStore(KeyFileSource(source.path)).write("alpha/token", "canary")
        report = self._doctor(healthy=False)
        assert report.startswith("FAIL")
        assert "control-database volume" in report

    @skip_if_root
    def test_unreadable_key_directory_fails_without_raising(self) -> None:
        locked = self.source.path.parent / "locked"
        locked.mkdir()
        source = KeyFileSource(locked / "master.key")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        with patch.dict(os.environ, {"T3_MASTER_KEY_SOURCE": source.label}):
            report = self._doctor(healthy=False)
        assert report.startswith("FAIL")
        assert source.label in report

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

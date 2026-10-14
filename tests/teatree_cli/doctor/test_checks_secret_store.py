import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.cli.doctor.checks_secret_store import check_secret_store
from teatree.core.sealed_store import DbStore
from teatree.utils.master_key import KeyFileSource


class TestSecretStoreDoctor(TestCase):
    def test_doctor_reports_key_source_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = KeyFileSource(Path(temporary) / "master.key")
            with patch.dict(os.environ, {"T3_MASTER_KEY_SOURCE": source.label}):
                output = io.StringIO()
                with redirect_stdout(output):
                    assert check_secret_store() is True
                assert "WARN" in output.getvalue()
                DbStore(source).write("alpha/token", "canary")
                output = io.StringIO()
                with redirect_stdout(output):
                    assert check_secret_store() is True
                assert source.load().key_id in output.getvalue()
                source.path.unlink()
                output = io.StringIO()
                with redirect_stdout(output):
                    assert check_secret_store() is False
                assert "FAIL" in output.getvalue()
                assert source.label in output.getvalue()


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

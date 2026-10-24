# test-path: cross-cutting
# Pins that `secrets status|ls|import-pass`, the health collector and doctor never create the master key.
import io
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.cli.doctor.checks_secret_store import check_secret_store
from teatree.core.factory.operational_health import collect_signals
from teatree.core.models.sealed_secret import SealedSecret
from teatree.core.sealed.store import DbStore
from teatree.utils.master_key import KeyFileSource


class TestSecretsCommandIsReadOnly(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = KeyFileSource(self.root / "master.key")
        (self.root / "store").mkdir()
        environment = patch.dict(
            os.environ, {"T3_MASTER_KEY_SOURCE": self.source.label, "PASSWORD_STORE_DIR": str(self.root / "store")}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def _run(self, *args: str) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            call_command("secrets", *args)
        return output.getvalue()

    def test_no_inspection_creates_the_key(self) -> None:
        self._run("status")
        self._run("ls")
        self._run("import-pass")
        collect_signals()
        with redirect_stdout(io.StringIO()):
            check_secret_store()
        assert not self.source.path.exists()

    def test_status_and_ls_show_names_and_key_ids_only(self) -> None:
        DbStore(self.source).write("team/token", "SENSITIVE_CANARY")
        DbStore(self.source).write("other/token", "SENSITIVE_CANARY")
        key_id = self.source.load().key_id
        status = self._run("status")
        assert f"key_id {key_id}: 2 rows" in status
        assert self._run("ls", "team/") == "team/token\n"
        assert "SENSITIVE_CANARY" not in status

    def test_status_exits_non_zero_for_every_unhealthy_key_verdict(self) -> None:
        for verdict in ("UNREADABLE", "WRONG_KEY", "TAMPERED", "INVALID"):
            with self.subTest(verdict=verdict):
                SealedSecret.objects.all().delete()
                self.source.path.unlink(missing_ok=True)
                DbStore(self.source).write("team/token", "SENSITIVE_CANARY")
                if verdict == "UNREADABLE":
                    self.source.path.unlink()
                elif verdict == "WRONG_KEY":
                    self.source.path.unlink()
                    self.source.create()
                elif verdict == "TAMPERED":
                    row = SealedSecret.objects.get(name="team/token")
                    flipped = bytearray(row.ciphertext)
                    flipped[0] ^= 1
                    SealedSecret.objects.filter(pk=row.pk).update(ciphertext=bytes(flipped))
                else:
                    self.source.path.write_text("not base64")
                output = io.StringIO()
                with redirect_stdout(output), pytest.raises(SystemExit) as raised:
                    call_command("secrets", "status")
                assert raised.value.code == 1
                assert f"health {verdict}" in output.getvalue()

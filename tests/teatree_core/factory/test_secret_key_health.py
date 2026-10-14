import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.factory.operational_health import HealthStatus, collect_signals, read_health, reconcile_health
from teatree.core.factory.secret_key_health import secret_key_signals
from teatree.core.models.sealed_secret import SealedSecret
from teatree.core.sealed_store import DbStore
from teatree.utils.master_key import KeyFileSource


class TestSecretKeyHealth(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = KeyFileSource(Path(temporary.name) / "master.key")
        environment = patch.dict(os.environ, {"T3_MASTER_KEY_SOURCE": self.source.label})
        environment.start()
        self.addCleanup(environment.stop)

    def test_missing_key_is_critical_and_read_health_is_red(self) -> None:
        assert secret_key_signals().signals == ()
        DbStore(self.source).write("alpha/token", "canary")
        assert secret_key_signals().signals == ()
        self.source.path.unlink()

        collection = collect_signals()
        matches = [signal for signal in collection.signals if signal.kind == "secret_key"]
        assert len(matches) == 1
        assert matches[0].severity == "critical"
        assert self.source.label in matches[0].summary
        assert "secret_key_signals" not in collection.unread
        reconcile_health()
        assert read_health().status == HealthStatus.RED

    def test_wrong_key_names_stored_and_active_ids(self) -> None:
        DbStore(self.source).write("alpha/token", "canary")
        original = self.source.load().key_id
        self.source.path.unlink()
        replacement = self.source.create().key_id
        assert replacement != original
        (signal,) = secret_key_signals().signals
        assert original in signal.summary
        assert replacement in signal.summary

    def test_tick_uses_key_ids_without_decrypting_rows(self) -> None:
        DbStore(self.source).write("alpha/token", "canary")
        with patch("teatree.core.models.sealed_secret.SealedSecret.reveal", side_effect=AssertionError("decrypted")):
            assert secret_key_signals().signals == ()

    def test_mode_error_is_unreadable_even_when_path_mentions_base64(self) -> None:
        source = KeyFileSource(self.source.path.parent / "base64-key")
        DbStore(source).write("alpha/token", "canary")
        source.path.chmod(0o644)
        assert SealedSecret.objects.key_health(source).status == "UNREADABLE"

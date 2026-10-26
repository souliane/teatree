import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import TestCase

from teatree.core.models.sealed_secret import SealedSecret
from teatree.utils.secrets import (
    PassStore,
    SecretNotFoundError,
    SecretStoreError,
    pass_entry_names,
    read_pass,
    read_pass_required,
    remove_pass,
    use_store,
    write_pass,
)
from tests._db_template import restore_from_template


def _pass_ran(*_args: object, **_kwargs: object) -> None:
    message = "pass ran"
    raise AssertionError(message)


class TestSealedSelector(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(
            os.environ,
            {
                "T3_SECRET_STORE": "sealed",
                "T3_MASTER_KEY_SOURCE": f"file:{temporary.name}/master.key",
                "PASSWORD_STORE_DIR": "",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        tripwire = patch("teatree.utils.secrets.run_bounded_group", side_effect=_pass_ran)
        tripwire.start()
        self.addCleanup(tripwire.stop)
        self.enterContext(use_store(None))

    def test_every_facade_call_reaches_the_rows_and_never_pass(self) -> None:
        assert write_pass("t3/test/a", "v1") is True
        assert read_pass("t3/test/a") == "v1"
        assert pass_entry_names("t3/test") == frozenset({"a"})
        assert remove_pass("t3/test/a") is True
        assert remove_pass("t3/test/a") is False

    def test_a_missing_required_secret_names_the_sealed_remedy(self) -> None:
        with pytest.raises(SecretNotFoundError) as raised:
            read_pass_required("t3/test/missing")
        assert "pass insert" not in str(raised.value)
        assert "secrets set t3/test/missing" in str(raised.value)

    def test_an_unreachable_database_raises_instead_of_reading_pass(self) -> None:
        with (
            patch.object(SealedSecret.objects, "filter", side_effect=OperationalError("unable to open database file")),
            pytest.raises(SecretStoreError, match="could not be read"),
        ):
            read_pass("t3/test/a")


class TestSelectorValues(TestCase):
    def test_an_unknown_value_names_the_setting(self) -> None:
        with (
            patch.dict(os.environ, {"T3_SECRET_STORE": "vault"}),
            use_store(None),
            pytest.raises(SecretStoreError, match="T3_SECRET_STORE"),
        ):
            read_pass("t3/test/a")

    def test_unset_keeps_the_pass_store(self) -> None:
        answer = CompletedProcess(args=[], returncode=0, stdout="from-pass\n", stderr="")
        with (
            patch.dict(os.environ, {"T3_SECRET_STORE": ""}),
            use_store(None),
            patch("teatree.utils.secrets.run_bounded_group", return_value=answer),
        ):
            assert read_pass("t3/test/a") == "from-pass"

    def test_an_installed_store_wins_over_the_selector(self) -> None:
        installed = PassStore()
        with patch.dict(os.environ, {"T3_SECRET_STORE": "vault"}), use_store(installed):
            answer = CompletedProcess(args=[], returncode=0, stdout="installed\n", stderr="")
            with patch("teatree.utils.secrets.run_bounded_group", return_value=answer):
                assert read_pass("t3/test/a") == "installed"


def test_a_cold_process_with_only_the_selector_answers_a_read(
    tmp_path: Path, migrated_db_template: Path | None
) -> None:
    sandbox = tmp_path / "xdg"
    (sandbox / "teatree").mkdir(parents=True)
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(sandbox),
        "T3_SECRET_STORE": "sealed",
        "T3_MASTER_KEY_SOURCE": f"file:{tmp_path}/master.key",
    }

    def run(code: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", code],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    if migrated_db_template is None:
        run(
            "from teatree.utils.django_bootstrap import ensure_django; ensure_django(); "
            "from django.core.management import call_command; call_command('migrate', verbosity=0)"
        )
    else:
        with closing(sqlite3.connect(sandbox / "teatree" / "db.sqlite3")) as target:
            restore_from_template(migrated_db_template, target)

    assert run("from teatree.utils.secrets import write_pass; print(write_pass('t3/test/a', 'v1'))") == "True"
    assert run("from teatree.utils.secrets import read_pass; print(read_pass('t3/test/a'))") == "v1"

# test-path: cross-cutting
# Pins that `secrets status|ls|import-pass`, the health collector and doctor never create the master key.
import io
import logging
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.cli.doctor.checks_secret_store import check_secret_store
from teatree.core.factory.operational_health import collect_signals
from teatree.core.models import ConfigSetting
from teatree.core.models.sealed_secret import SealedSecret
from teatree.core.sealed.store import DbStore
from teatree.utils.master_key import KeyFileSource
from teatree.utils.secrets import PassStore, use_store


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

    def test_status_names_the_selected_store(self) -> None:
        with patch.dict(os.environ, {"T3_SECRET_STORE": ""}):
            assert self._run("status").splitlines()[0] == "store pass"
        with patch.dict(os.environ, {"T3_SECRET_STORE": "sealed"}):
            assert self._run("status").splitlines()[0] == "store sealed"

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


CANARY = "canary-5538-ünïcode"


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestSecretsCommandsKeepValuesOffEveryChannel(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = KeyFileSource(self.root / "master.key")
        self.enterContext(patch.dict(os.environ, {"T3_MASTER_KEY_SOURCE": self.source.label}))
        self.enterContext(use_store(DbStore(self.source)))

    def _run(self, *args: str, stdin: str = "") -> tuple[str, str, int]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with (
            self.assertLogs(level=logging.DEBUG) as logs,
            redirect_stdout(out),
            redirect_stderr(err),
            patch.object(sys, "stdin", io.StringIO(stdin)),
        ):
            logging.getLogger("probe").debug("start")
            try:
                call_command("secrets", *args)
            except SystemExit as raised:
                code = int(raised.code or 0)
        assert CANARY not in "\n".join(logs.output)
        return out.getvalue(), err.getvalue(), code

    def test_set_stores_the_value_and_prints_only_the_store_and_the_name(self) -> None:
        out, err, code = self._run("set", "team/token", stdin=f"{CANARY}\n")
        assert code == 0
        assert out.strip() == "stored team/token in the sealed secret store"
        assert CANARY not in out + err
        assert DbStore(self.source).read("team/token") == CANARY

    def test_set_refuses_an_empty_value_and_stores_nothing(self) -> None:
        _, err, code = self._run("set", "team/token", stdin="\n")
        assert code == 1
        assert "empty" in err
        assert not SealedSecret.objects.exists()

    def test_rm_removes_the_row(self) -> None:
        DbStore(self.source).write("team/token", CANARY)
        out, _, code = self._run("rm", "team/token")
        assert (code, out.strip()) == (0, "removed team/token from the sealed secret store")
        assert not SealedSecret.objects.exists()

    def test_get_through_a_pipe_returns_the_first_line_and_exit_codes_distinguish_the_failures(self) -> None:
        DbStore(self.source).write("team/token", f"{CANARY}\nsecond line")
        out, _, code = self._run("get", "team/token")
        assert (code, out) == (0, f"{CANARY}\n")
        assert self._run("get", "team/absent")[2] == 1
        row = SealedSecret.objects.get(name="team/token")
        SealedSecret.objects.filter(pk=row.pk).update(ciphertext=bytes(len(row.ciphertext)))
        out, err, code = self._run("get", "team/token")
        assert code == 2
        assert CANARY not in out + err

    def test_get_exits_one_for_a_missing_pass_executable_like_the_shell_reader(self) -> None:
        with use_store(PassStore()), patch("teatree.utils.secrets.run_bounded_group", side_effect=FileNotFoundError):
            out, _, code = self._run("get", "team/token")
        assert (code, out) == (1, "")

    def test_get_refuses_when_stdout_is_a_terminal(self) -> None:
        DbStore(self.source).write("team/token", CANARY)
        terminal = _Terminal()
        with redirect_stdout(terminal), redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as raised:
            call_command("secrets", "get", "team/token")
        assert raised.value.code == 3
        assert CANARY not in terminal.getvalue()

    def test_exec_delivers_the_value_to_the_child_environment_only(self) -> None:
        DbStore(self.source).write("team/token", CANARY)
        sink = self.root / "child-saw"
        script = f"import os, pathlib; pathlib.Path({str(sink)!r}).write_text(os.environ['SECRET_VALUE'])"
        out, err, code = self._run("exec", "--env", "SECRET_VALUE=team/token", "--", sys.executable, "-c", script)
        assert code == 0
        assert sink.read_text() == CANARY
        assert CANARY not in out + err
        assert "SECRET_VALUE" not in os.environ

    def test_verify_lists_names_and_statuses_only_and_exits_one_only_on_an_unreadable_row(self) -> None:
        ConfigSetting.objects.create(scope="global", key="github_token_pass_key", value="team/github")
        ConfigSetting.objects.create(scope="global", key="gitlab_token_pass_key", value="team/absent")
        DbStore(self.source).write("team/github", CANARY)
        out, err, code = self._run("verify")
        assert code == 0
        assert "ok\tteam/github" in out
        assert "absent\tteam/absent" in out
        assert CANARY not in out + err
        row = SealedSecret.objects.get(name="team/github")
        SealedSecret.objects.filter(pk=row.pk).update(ciphertext=bytes(len(row.ciphertext)))
        out, err, code = self._run("verify")
        assert code == 1
        assert "unreadable\tteam/github" in out
        assert CANARY not in out + err

    def test_set_rm_and_verify_act_on_sealed_rows_whatever_the_selector_says(self) -> None:
        with patch.dict(os.environ, {"T3_SECRET_STORE": "pass"}):
            self._run("set", "team/token", stdin=f"{CANARY}\n")
            assert DbStore(self.source).read("team/token") == CANARY
            self._run("rm", "team/token")
        assert not SealedSecret.objects.exists()

    def test_set_backs_up_a_replaced_value_and_reports_replacement(self) -> None:
        DbStore(self.source).write("team/token", CANARY)
        out, err, code = self._run("set", "team/token", stdin="new-value\n")
        backups = SealedSecret.objects.filter(name__startswith="team/token.bak-")
        assert code == 0
        assert "replaced team/token" in out
        assert CANARY not in out + err
        assert backups.count() == 1
        assert DbStore(self.source).read(backups.get().name) == CANARY
        assert DbStore(self.source).read("team/token") == "new-value"

    def test_two_rotations_in_one_second_keep_both_previous_values(self) -> None:
        DbStore(self.source).write("team/token", "first")
        with patch("teatree.core.management.commands.secrets.timezone.now") as now:
            now.return_value = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
            self._run("set", "team/token", stdin="second\n")
            self._run("set", "team/token", stdin="third\n")
        names = SealedSecret.objects.filter(name__startswith="team/token.bak-").values_list("name", flat=True)
        assert {DbStore(self.source).read(name) for name in names} == {"first", "second"}

    def test_bad_master_key_source_is_a_typed_refusal_for_operator_commands(self) -> None:
        with patch.dict(os.environ, {"T3_MASTER_KEY_SOURCE": "bad"}):
            for args in (("set", "team/token"), ("verify",), ("import-pass", "--apply")):
                with self.subTest(args=args):
                    out, err, code = self._run(*args, stdin="new-value\n")
                    assert code == 2
                    assert not out
                    assert "T3_MASTER_KEY_SOURCE" in err
                    assert "Traceback" not in err

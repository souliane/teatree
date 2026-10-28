import io
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase

from teatree.core.models import ConfigSetting, SealedSecret, SecretCutover
from teatree.core.sealed.pass_import import CopyOutcome, ImportEntry, apply_import, build_import_plan
from teatree.core.sealed.references import referenced_entry_names
from teatree.core.sealed.store import DbStore
from teatree.utils.master_key import KeyFileSource
from teatree.utils.secret_backups import backup_name
from teatree.utils.secrets import PassStore
from tests.secret_memory_store import MemoryStore


class TestSecretImportPlan(TestCase):
    def test_project_pass_paths_are_referenced(self) -> None:
        ConfigSetting.objects.create(
            scope="global", key="gitlab_read_token_pass_paths", value={"group/repo": "team/read-only"}
        )
        with patch("teatree.core.sealed.references.get_all_overlays", return_value={}):
            assert "team/read-only" in referenced_entry_names()

    def test_overlay_declared_pass_key_is_referenced(self) -> None:
        config = SimpleNamespace(
            declared_credentials=lambda: frozenset({"artifact_token"}),
            resolve_pass_key=lambda name: SimpleNamespace(value="custom/artifact" if name == "artifact_token" else ""),
            slack_token_ref="",
            user_token_ref="",
        )
        with patch(
            "teatree.core.sealed.references.get_all_overlays", return_value={"custom": SimpleNamespace(config=config)}
        ):
            assert "custom/artifact" in referenced_entry_names()

    def test_names_only_plan_and_command_never_write_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            password_store = root / "store"
            names = (
                "team/github",
                "team/github.bak-20260101T000000Z",
                "external/private",
                "anthropic/work/oauth-token",
                "teatree/github-webhook/repository-4",
                "teatree/codex/ro-executor-auth-json-b64",
                "slack/user-oauth-token",
                "teatree/admin-password",
                "custom/admin",
                "teatree/slack-app-config-issued-at-writability-probe",
            )
            for name in names:
                file = password_store / f"{name}.gpg"
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("encrypted placeholder")
            argv_log = root / "argv"
            fake = root / "pass"
            fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{argv_log}'\nprintf 'SENSITIVE_CANARY\\n'\n")
            fake.chmod(0o700)
            ConfigSetting.objects.create(scope="global", key="github_token_pass_key", value="team/github")
            ConfigSetting.objects.create(scope="global", key="gitlab_token_pass_key", value="team/absent")
            with patch.dict(
                os.environ,
                {
                    "PATH": f"{root}:{os.environ['PATH']}",
                    "PASSWORD_STORE_DIR": str(password_store),
                    "T3_ADMIN_PASSWORD_PASS_PATH": "custom/admin",
                },
            ):
                entries = {entry.name: entry for entry in build_import_plan(store=PassStore())}
                output = io.StringIO()
                errors = io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    call_command("secrets", "import-pass")
            assert (entries["team/github"].destination, entries["team/github"].readability) == ("row", "readable")
            assert (entries["team/absent"].destination, entries["team/absent"].readability) == ("missing", "missing")
            assert entries["team/github.bak-20260101T000000Z"].destination == "row"
            assert entries["external/private"].destination == "external"
            assert entries["anthropic/work/oauth-token"].destination == "row"
            assert entries["teatree/github-webhook/repository-4"].destination == "row"
            assert entries["teatree/codex/ro-executor-auth-json-b64"].destination == "row"
            assert entries["slack/user-oauth-token"].destination == "row"
            assert entries["teatree/admin-password"].destination == "row"
            assert entries["custom/admin"].destination == "row"
            assert entries["teatree/slack-app-config-issued-at-writability-probe"].destination == "drop"
            assert "SENSITIVE_CANARY" not in output.getvalue() + errors.getvalue()
            assert "team/github" in output.getvalue()
            assert all(line.startswith("show ") for line in argv_log.read_text().splitlines())
            assert "show external/private" not in argv_log.read_text()


class TestPasswordStoreRoot(TestCase):
    def test_a_symlinked_root_is_accepted_and_inner_symlinks_are_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            real = Path(temporary) / "real"
            (real / "team").mkdir(parents=True)
            (real / "team" / "token.gpg").write_text("encrypted placeholder")
            (real / "team" / "alias.gpg").symlink_to(real / "team" / "token.gpg")
            link = Path(temporary) / "link"
            link.symlink_to(real)
            names = {entry.name for entry in build_import_plan(store=MemoryStore(), root=link)}
        assert "team/token" in names
        assert "team/alias" not in names

    def test_an_unreadable_root_exits_one_with_one_line_and_no_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "absent"
            errors = io.StringIO()
            with (
                patch.dict(os.environ, {"PASSWORD_STORE_DIR": str(missing)}),
                redirect_stderr(errors),
                pytest.raises(SystemExit) as raised,
            ):
                call_command("secrets", "import-pass")
        assert raised.value.code == 1
        assert errors.getvalue().splitlines() == [f"password store directory is unreadable: {missing}"]


FAKE_PASS = """#!/bin/sh
printf '%s\\n' "$*" >> "$PASS_LOG"
entry() { printf '%s/%s.gpg' "$PASSWORD_STORE_DIR" "$1"; }
[ "$1" = show ] || exit 2
[ -f "$(entry "$2")" ] || exit 1
[ ! -f "$(entry "$2").broken" ] || exit 2
cat "$(entry "$2")"
"""
APPLY_CANARY = "apply-canary-5538"


class TestApplyImport(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store_root = self.root / "store"
        self.pass_log = self.root / "pass.log"
        self.pass_log.write_text("")
        binary = self.root / "bin"
        binary.mkdir()
        (binary / "pass").write_text(FAKE_PASS)
        (binary / "pass").chmod(0o700)
        self.source = KeyFileSource(self.root / "master.key")
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "PATH": f"{binary}:{os.environ['PATH']}",
                    "PASSWORD_STORE_DIR": str(self.store_root),
                    "PASS_LOG": str(self.pass_log),
                    "T3_MASTER_KEY_SOURCE": self.source.label,
                    "T3_SECRET_STORE": "",
                },
            )
        )
        now = datetime.now(UTC)
        self.recent = backup_name("teatree/slack-app-config-token", now - timedelta(days=1))
        self.expired = backup_name("teatree/slack-app-config-token", now - timedelta(days=100))
        self.values = {
            "team/github": f"{APPLY_CANARY}-github\n",
            "teatree/slack-app-config-token": f"{APPLY_CANARY}-access\n",
            "teatree/slack-app-config-refresh": f"{APPLY_CANARY}-refresh\n",
            "teatree/slack-app-config-issued-at": "2026-10-25T10:00:00+00:00\n",
            self.recent: f"{APPLY_CANARY}-previous\n",
            self.expired: f"{APPLY_CANARY}-ancient\n",
            "external/private": f"{APPLY_CANARY}-external\n",
        }
        for name, value in self.values.items():
            entry = self.store_root / f"{name}.gpg"
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(value)
        ConfigSetting.objects.create(scope="global", key="github_token_pass_key", value="team/github")

    def _apply(self) -> tuple[str, str, int]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                call_command("secrets", "import-pass", "--apply")
            except SystemExit as raised:
                code = int(raised.code or 0)
        assert APPLY_CANARY not in out.getvalue() + err.getvalue()
        return out.getvalue(), err.getvalue(), code

    def test_every_row_opens_to_the_same_bytes_and_the_marker_follows_the_read_back(self) -> None:
        out, _, code = self._apply()
        assert code == 0
        statuses = dict(line.split("\t")[::-1] for line in out.splitlines())
        assert statuses["team/github"] == "copied"
        assert statuses[self.recent] == "copied"
        assert statuses[self.expired] == "expired"
        store = DbStore(self.source)
        for name, value in self.values.items():
            if name in {"external/private", self.expired}:
                assert store.read(name) is None
            else:
                assert store.read(name) == value
        assert SecretCutover.objects.filter(pk=1).exists()

    def test_a_second_run_changes_nothing(self) -> None:
        self._apply()
        out, _, code = self._apply()
        assert code == 0
        assert {line.split("\t")[0] for line in out.splitlines()} == {"unchanged", "expired"}
        assert SecretCutover.objects.count() == 1

    def test_apply_does_not_decrypt_a_row_during_inventory(self) -> None:
        _, _, code = self._apply()
        assert code == 0
        calls = self.pass_log.read_text().splitlines()
        assert calls.count("show team/github") == 2

    def test_existing_newer_backups_do_not_make_an_older_copy_fail(self) -> None:
        store = DbStore(self.source)
        now = datetime.now(UTC)
        for age in (0, 1, 2):
            store.write(backup_name("teatree/slack-app-config-token", now - timedelta(hours=age)), "newer")
        out, _, code = self._apply()
        assert code == 0
        assert f"expired\t{self.recent}" in out
        assert store.read(self.recent) is None
        assert SecretCutover.objects.filter(pk=1).exists()

    def test_an_unreadable_entry_exits_non_zero_and_writes_no_marker(self) -> None:
        (self.store_root / "team" / "github.gpg.broken").write_text("")
        out, _, code = self._apply()
        assert code == 1
        assert "failed\tteam/github" in out
        assert not SecretCutover.objects.exists()

    def test_an_empty_entry_exits_non_zero_and_writes_no_marker(self) -> None:
        (self.store_root / "team" / "github.gpg").write_text("\n")
        out, _, code = self._apply()
        assert code == 1
        assert "empty\tteam/github" in out
        assert not SecretCutover.objects.exists()

    def test_a_selector_of_sealed_refuses_before_reading_pass(self) -> None:
        with patch.dict(os.environ, {"T3_SECRET_STORE": "sealed"}):
            _, err, code = self._apply()
        assert code == 1
        assert "T3_SECRET_STORE" in err
        assert self.pass_log.read_text() == ""
        assert not SealedSecret.objects.exists()

    def test_a_key_inside_the_control_database_volume_refuses_before_reading_pass(self) -> None:
        volume = self.root / "control"
        volume.mkdir()
        environment = {"TEATREE_ROLE": "worker", "T3_CONTROL_DB_DIR": str(volume), "T3_MASTER_KEY_SOURCE": ""}
        with patch.dict(os.environ, environment):
            _, err, code = self._apply()
        assert code == 1
        assert "control-database volume" in err
        assert self.pass_log.read_text() == ""


class _ChangingSource(MemoryStore):
    """A source whose value moves between the copy and the final read-back, like a rotating pair."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def read(self, key: str) -> str | None:
        self.reads += 1
        return f"value-{self.reads}\n"


def test_a_value_that_moved_after_the_copy_fails_the_second_read_back() -> None:
    plan = (ImportEntry("team/github", "row", "readable"),)
    stable = MemoryStore()
    stable.entries["team/github"] = "steady\n"
    now = datetime.now(UTC)
    assert [outcome.status for outcome in apply_import(stable, MemoryStore(), plan, now=now)] == ["copied"]
    assert [outcome.status for outcome in apply_import(_ChangingSource(), MemoryStore(), plan, now=now)] == ["failed"]


def test_backup_retention_is_ranked_separately_for_each_base() -> None:
    now = datetime.now(UTC)
    names = [
        backup_name(base, now - timedelta(days=age))
        for base in ("anthropic/a/oauth", "teatree/slack-app-config-token")
        for age in (1, 2)
    ]
    source = MemoryStore()
    source.entries.update(dict.fromkeys(names, "previous credential"))
    plan = tuple(ImportEntry(name, "row", "readable") for name in names)
    outcomes = apply_import(source, MemoryStore(), plan, now=now)
    assert {outcome.name: outcome.status for outcome in outcomes} == dict.fromkeys(names, "copied")


def test_a_re_run_keeps_every_backup_the_first_run_retained() -> None:
    now = datetime.now(UTC)
    names = [backup_name("teatree/slack-app-config-token", now - timedelta(days=age)) for age in (1, 2, 3)]
    source = MemoryStore()
    source.entries.update(dict.fromkeys(names, "previous credential"))
    plan = tuple(ImportEntry(name, "row", "readable") for name in names)
    target = MemoryStore()
    assert {outcome.status for outcome in apply_import(source, target, plan, now=now)} == {"copied"}
    assert {outcome.status for outcome in apply_import(source, target, plan, now=now)} == {"unchanged"}
    assert set(target.entries) == set(names)


def test_a_database_failure_is_reported_per_name_without_aborting_the_summary() -> None:
    class LockedStore(MemoryStore):
        def write(self, key: str, value: str) -> bool:
            message = "database is locked"
            raise OperationalError(message)

    source = MemoryStore()
    source.entries["team/token"] = "value"
    plan = (ImportEntry("team/token", "row", "readable"),)
    outcomes = apply_import(source, LockedStore(), plan, now=datetime.now(UTC))
    assert outcomes == (CopyOutcome("failed", "team/token"),)

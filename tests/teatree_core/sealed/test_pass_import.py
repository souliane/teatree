import io
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.core.sealed.pass_import import build_import_plan, referenced_entry_names
from teatree.utils.secrets import PassStore


class TestSecretImportPlan(TestCase):
    def test_overlay_declared_pass_key_is_referenced(self) -> None:
        config = SimpleNamespace(
            declared_credentials=lambda: frozenset({"artifact_token"}),
            resolve_pass_key=lambda name: SimpleNamespace(value="custom/artifact" if name == "artifact_token" else ""),
            slack_token_ref="",
            user_token_ref="",
        )
        with patch(
            "teatree.core.sealed.pass_import.get_all_overlays", return_value={"custom": SimpleNamespace(config=config)}
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

"""The Codex auth bootstrap stores a whole cache without printing it."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase
from typer.testing import CliRunner

from teatree.agents.codex_auth_cache import CodexAuthCacheError
from teatree.agents.skill_routing import (
    QUOTA_AUTH_HOLD,
    cached_unavailable_reason,
    clear_route_availability_cache,
    record_route_unavailable,
)
from teatree.cli.codex import codex_app
from teatree.config.agent_spawn import AgentRouteCandidate
from teatree.core.models import AgentRouteAvailability

_FORGET_HOLDS = "teatree.cli.codex_auth._forget_codex_holds"


def test_codex_auth_import_help_uses_a_portable_default_path() -> None:
    result = CliRunner().invoke(codex_app, ["auth", "import", "--help"])

    assert result.exit_code == 0
    assert "~/.codex/auth.json" in result.stdout
    assert str(Path.home() / ".codex") not in result.stdout


def test_codex_auth_import_reads_the_whole_file_and_names_the_pass_entry(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    secret = '{"tokens":{"access_token":"do-not-print"}}'
    auth_path.write_text(secret, encoding="utf-8")

    with patch("teatree.cli.codex_auth.store_auth_cache_from_reader") as store, patch(_FORGET_HOLDS):
        result = CliRunner().invoke(codex_app, ["auth", "import", "--from", str(auth_path)])

    assert result.exit_code == 0
    assert store.call_args.args[0]() == secret.encode()
    assert "teatree/codex/auth-json-b64" in result.stdout
    assert "do-not-print" not in result.stdout


def test_codex_auth_import_reports_safe_validation_error(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"token":"do-not-print"}', encoding="utf-8")

    with patch(
        "teatree.cli.codex_auth.store_auth_cache_from_reader",
        side_effect=CodexAuthCacheError.invalid_json(),
    ):
        result = CliRunner().invoke(codex_app, ["auth", "import", "--from", str(auth_path)])

    assert result.exit_code != 0
    assert "do-not-print" not in result.stdout


def test_codex_auth_import_accepts_bounded_stdin_without_printing_it() -> None:
    secret = '{"tokens":{"access_token":"do-not-print"}}'

    with patch("teatree.cli.codex_auth.store_auth_cache") as store, patch(_FORGET_HOLDS):
        result = CliRunner().invoke(codex_app, ["auth", "import", "--from", "-"], input=secret)

    assert result.exit_code == 0
    assert store.call_args.args == (secret.encode(),)
    assert "teatree/codex/auth-json-b64" in result.stdout
    assert "do-not-print" not in result.stdout


def test_codex_auth_import_rejects_empty_stdin() -> None:
    with patch("teatree.cli.codex_auth.store_auth_cache") as store:
        result = CliRunner().invoke(codex_app, ["auth", "import", "--from", "-"], input="")

    assert result.exit_code != 0
    assert "empty" in result.stderr.lower()
    store.assert_not_called()


def test_codex_auth_import_rejects_stdin_above_the_bounded_limit() -> None:
    with (
        patch("teatree.cli.codex_auth._MAX_AUTH_BYTES", 8),
        patch("teatree.cli.codex_auth.store_auth_cache") as store,
    ):
        result = CliRunner().invoke(codex_app, ["auth", "import", "--from", "-"], input="do-not-print")

    assert result.exit_code != 0
    assert "too large" in result.stderr.lower()
    assert "do-not-print" not in result.stdout + result.stderr
    store.assert_not_called()


def test_codex_auth_import_does_not_echo_invalid_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_CODEX_HOME", str(tmp_path))
    result = CliRunner().invoke(
        codex_app,
        ["auth", "import", "--from", "-"],
        input='{"token":"do-not-print"',
    )

    assert result.exit_code != 0
    assert "not valid JSON" in result.stderr
    assert "do-not-print" not in result.stdout + result.stderr


class TestImportClearsTheCodexHold(TestCase):
    def test_import_drops_the_codex_availability_rows_and_a_workers_cached_hold(self) -> None:
        codex = AgentRouteCandidate("codex_app_server", "gpt-6-sol")
        claude = AgentRouteCandidate("claude_sdk", "claude-opus-5-5")
        clear_route_availability_cache()
        self.addCleanup(clear_route_availability_cache)
        record_route_unavailable("acme", codex, "auth failed", phase="coding", retry_after=QUOTA_AUTH_HOLD)
        record_route_unavailable("acme", claude, "quota", phase="coding", retry_after=QUOTA_AUTH_HOLD)
        auth_path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "auth.json"
        auth_path.write_text('{"tokens":{}}', encoding="utf-8")

        with patch("teatree.cli.codex_auth.store_auth_cache_from_reader"):
            result = CliRunner().invoke(codex_app, ["auth", "import", "--from", str(auth_path)])

        assert result.exit_code == 0
        assert not AgentRouteAvailability.objects.filter(harness="codex_app_server").exists()
        assert AgentRouteAvailability.objects.filter(harness="claude_sdk").exists()
        assert cached_unavailable_reason("acme", codex, lambda: None, phase="coding") is None
        assert cached_unavailable_reason("acme", claude, lambda: None, phase="coding") == "quota"

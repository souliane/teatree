"""The Codex auth bootstrap stores a whole cache without printing it."""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.agents.codex_auth_cache import CodexAuthCacheError
from teatree.cli.codex import codex_app


def test_codex_auth_import_help_uses_a_portable_default_path() -> None:
    result = CliRunner().invoke(codex_app, ["auth", "import", "--help"])

    assert result.exit_code == 0
    assert "~/.codex/auth.json" in result.stdout
    assert str(Path.home() / ".codex") not in result.stdout


def test_codex_auth_import_reads_the_whole_file_and_names_the_pass_entry(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    secret = '{"tokens":{"access_token":"do-not-print"}}'
    auth_path.write_text(secret, encoding="utf-8")

    with patch("teatree.cli.codex_auth.store_auth_cache_from_reader") as store:
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

    with patch("teatree.cli.codex_auth.store_auth_cache") as store:
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


def test_codex_auth_import_does_not_echo_invalid_stdin() -> None:
    result = CliRunner().invoke(
        codex_app,
        ["auth", "import", "--from", "-"],
        input='{"token":"do-not-print"',
    )

    assert result.exit_code != 0
    assert "not valid JSON" in result.stderr
    assert "do-not-print" not in result.stdout + result.stderr

import os
from subprocess import CompletedProcess

import pytest

from teatree.utils import db
from teatree.utils import run as utils_run_mod


def test_db_restore_uses_pg_restore_when_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_USER", "postgres")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        commands.append(args)
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 0, "toc", "")
        if args[0] == "pg_restore":
            return CompletedProcess(args, 0, "", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    db.db_restore("wt_123", "/tmp/dump.dump")

    assert commands[0][:2] == ["dropdb", "-h"]
    assert commands[1][:2] == ["createdb", "-h"]
    assert commands[2] == ["pg_restore", "-l", "/tmp/dump.dump"]
    assert commands[3][0] == "pg_restore"


def test_drop_db_forwards_host_and_env_like_db_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """``drop_db`` must accept and forward ``host``/``env`` symmetrically with ``db_exists``.

    The create/import counterpart (``db_exists``) connects with the
    worktree's resolved host and env; ``drop_db`` connected only with the
    resolved user and fell back to the bare process-env host/env. On a
    host where the worktree's postgres is not on ``localhost`` (or needs a
    distinct PGPORT/PGPASSWORD), that asymmetry makes the drop target the
    wrong server.
    """
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "default.localhost")

    commands: list[list[str]] = []
    seen_env: list[dict[str, str] | None] = []

    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        commands.append(args)
        seen_env.append(env)
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    db.drop_db("wt_88", user="db_superuser", host="db.internal", env={"PGPORT": "5544"})

    assert commands[0][0] == "dropdb"
    assert commands[0][2] == "db.internal"
    assert commands[0][4] == "db_superuser"
    assert seen_env[0] == {"PGPORT": "5544"}


def test_drop_db_defaults_to_process_env_host_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ``host``/``env`` passed, ``drop_db`` keeps the legacy default behavior."""
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "default.localhost")
    monkeypatch.setenv("POSTGRES_USER", "postgres")

    commands: list[list[str]] = []

    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        commands.append(args)
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    db.drop_db("wt_89")

    assert commands[0][2] == "default.localhost"
    assert commands[0][4] == "postgres"


def test_db_helpers_cover_env_exists_and_psql_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_USER", "worker")

    commands: list[list[str]] = []

    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        commands.append(args)
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 1, "", "")
        if args[0] == "psql" and "-lqt" in args:
            return CompletedProcess(args, 0, "wt_42 | owner\n", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    assert db.pg_env().get("PGPASSWORD") is None
    assert db.pg_host() == "db.internal"
    assert db.pg_user() == "worker"
    db.db_restore("wt_42", "/tmp/dump.sql")
    assert db.db_exists("wt_42") is True
    assert commands[3][0] == "psql"


def test_db_restore_raises_when_restore_commands_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 0, "toc", "")
        if args[0] == "pg_restore":
            return CompletedProcess(args, 1, "", "")
        if args[0] == "psql":
            return CompletedProcess(args, 1, "", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="pg_restore failed"):
        db.db_restore("wt_55", "/tmp/dump.dump")


def test_db_restore_raises_when_psql_restore_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 1, "", "")
        if args[0] == "psql":
            return CompletedProcess(args, 1, "", "")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="psql restore failed"):
        db.db_restore("wt_56", "/tmp/dump.sql")


def test_db_restore_detects_truncated_pg_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 0, "TOC", "")
        if args[0] == "pg_restore" and "-d" in args:
            return CompletedProcess(args, 0, "", "WARNING: could not read data")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Corrupt or truncated dump"):
        db.db_restore("wt_70", "/tmp/dump.pgdump")


def test_db_restore_detects_truncated_psql(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        **_kwargs: object,
    ) -> CompletedProcess[str]:
        if args[:2] == ["pg_restore", "-l"]:
            return CompletedProcess(args, 1, "", "")
        if args[0] == "psql":
            return CompletedProcess(args, 0, "", "unexpected EOF on client connection")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(utils_run_mod.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Corrupt or truncated dump"):
        db.db_restore("wt_71", "/tmp/dump.sql")


def test_pg_env_includes_port_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    env = db.pg_env()

    assert env["PGPORT"] == "5433"
    assert env["PGPASSWORD"] == "secret"


def test_pg_env_omits_port_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    env = db.pg_env()

    assert "PGPORT" not in env


def test_pg_env_resolves_password_via_pass_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pg_env`` reads the password from ``pass`` when ``POSTGRES_PASSWORD_PASS_KEY`` is set.

    The literal value never appears in ``os.environ`` — the only secret
    delivery channel is the in-memory ``PGPASSWORD`` value placed on the
    returned ``env`` dict.
    """
    from unittest.mock import patch  # noqa: PLC0415

    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD_PASS_KEY", "teatree/wt/42/postgres")
    with patch("teatree.utils.postgres_secret.secrets.read_pass", return_value="from-pass"):
        env = db.pg_env()
    assert env["PGPASSWORD"] == "from-pass"
    # The literal value must not be planted into the original process env.
    assert os.environ.get("POSTGRES_PASSWORD", "") == ""

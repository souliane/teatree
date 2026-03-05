"""Tests for _db.py — Postgres database helpers."""

from unittest.mock import MagicMock, patch

import pytest
from lib.db import db_exists, db_restore, pg_env

pytestmark = pytest.mark.usefixtures("pg_env")


class TestPgEnv:
    def test_includes_pgport_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        env = pg_env()
        assert env["PGPORT"] == "5433"

    def test_excludes_pgport_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POSTGRES_PORT", raising=False)
        env = pg_env()
        assert "PGPORT" not in env


class TestDbExists:
    def test_returns_true_when_db_listed(self) -> None:
        psql_output = " postgres  | testuser | UTF8\n wt_1234   | testuser | UTF8\n template0 | testuser | UTF8\n"
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=psql_output)
            assert db_exists("wt_1234") is True

    def test_returns_false_when_db_not_listed(self) -> None:
        psql_output = " postgres  | testuser | UTF8\n template0 | testuser | UTF8\n"
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=psql_output)
            assert db_exists("wt_9999") is False

    def test_returns_false_when_psql_fails(self) -> None:
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert db_exists("anything") is False

    def test_does_not_match_partial_names(self) -> None:
        psql_output = " wt_1234_globex | testuser | UTF8\n"
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=psql_output)
            assert db_exists("wt_1234") is False


class TestDbRestore:
    def test_binary_dump_uses_pg_restore(self) -> None:
        with patch("lib.db.subprocess.run") as mock_run:
            # pg_restore -l succeeds → binary format
            mock_run.side_effect = [
                MagicMock(returncode=0),  # dropdb
                MagicMock(returncode=0),  # createdb
                MagicMock(returncode=0, stderr=""),  # pg_restore -l
                MagicMock(returncode=0, stderr=""),  # pg_restore
            ]
            db_restore("mydb", "/dump.pgsql")

            calls = mock_run.call_args_list
            assert calls[0].args[0][0] == "dropdb"
            assert calls[1].args[0][0] == "createdb"
            assert calls[2].args[0][:2] == ["pg_restore", "-l"]
            assert calls[3].args[0][0] == "pg_restore"
            assert "--no-owner" in calls[3].args[0]

    def test_sql_dump_uses_psql(self) -> None:
        with patch("lib.db.subprocess.run") as mock_run:
            # pg_restore -l fails → SQL format
            mock_run.side_effect = [
                MagicMock(returncode=0),  # dropdb
                MagicMock(returncode=0),  # createdb
                MagicMock(returncode=1, stderr=""),  # pg_restore -l → fails
                MagicMock(returncode=0, stderr=""),  # psql
            ]
            db_restore("mydb", "/dump.sql")

            calls = mock_run.call_args_list
            assert calls[3].args[0][0] == "psql"
            assert "-f" in calls[3].args[0]

    def test_binary_restore_failure_raises(self) -> None:
        with patch("lib.db.subprocess.run") as mock_run:
            # pg_restore -l succeeds (binary), but restore fails with no stderr
            mock_run.side_effect = [
                MagicMock(returncode=0),  # dropdb
                MagicMock(returncode=0),  # createdb
                MagicMock(returncode=0, stderr=""),  # pg_restore -l
                MagicMock(returncode=1, stderr=""),  # pg_restore fails, no detail
            ]
            with pytest.raises(RuntimeError, match="pg_restore failed"):
                db_restore("mydb", "/broken.pgsql")

    def test_binary_restore_failure_includes_stderr(self) -> None:
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # dropdb
                MagicMock(returncode=0),  # createdb
                MagicMock(returncode=0, stderr=""),  # pg_restore -l
                MagicMock(returncode=1, stderr="connection refused"),
            ]
            with pytest.raises(RuntimeError, match="connection refused"):
                db_restore("mydb", "/broken.pgsql")

    def test_truncated_dump_detected_during_restore(self) -> None:
        """pg_restore may exit 0 but report truncation on stderr."""
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # dropdb
                MagicMock(returncode=0),  # createdb
                MagicMock(returncode=0, stderr=""),  # pg_restore -l
                MagicMock(
                    returncode=0,
                    stderr="pg_restore: error: could not read from input file: end of file",
                ),  # pg_restore succeeds exit code but truncated
            ]
            with pytest.raises(RuntimeError, match="pg_restore failed"):
                db_restore("mydb", "/truncated.pgsql")

    def test_truncated_dump_detected_during_toc_listing(self) -> None:
        """pg_restore -l may succeed (rc=0) but show truncation on stderr."""
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # dropdb
                MagicMock(returncode=0),  # createdb
                MagicMock(
                    returncode=0,
                    stderr="pg_restore: warning: could not read some data",
                ),  # pg_restore -l
            ]
            with pytest.raises(RuntimeError, match="truncated"):
                db_restore("mydb", "/truncated.pgsql")

    def test_sql_restore_failure_raises(self) -> None:
        with patch("lib.db.subprocess.run") as mock_run:
            # pg_restore -l fails (plain SQL), then psql restore fails
            mock_run.side_effect = [
                MagicMock(returncode=0),  # dropdb
                MagicMock(returncode=0),  # createdb
                MagicMock(returncode=1, stderr=""),  # pg_restore -l -> plain SQL
                MagicMock(returncode=1, stderr=""),  # psql restore fails
            ]
            with pytest.raises(RuntimeError, match="psql restore failed"):
                db_restore("mydb", "/broken.sql")

    def test_passes_correct_host_and_user(self) -> None:
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            db_restore("mydb", "/dump.pgsql")

            # Check dropdb args
            dropdb_args = mock_run.call_args_list[0].args[0]
            assert "-h" in dropdb_args
            idx = dropdb_args.index("-h")
            assert dropdb_args[idx + 1] == "localhost"
            assert "-U" in dropdb_args
            idx = dropdb_args.index("-U")
            assert dropdb_args[idx + 1] == "testuser"

    def test_sets_pgpassword_in_env(self) -> None:
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            db_restore("mydb", "/dump.pgsql")

            # All subprocess calls should have PGPASSWORD in env
            for c in mock_run.call_args_list:
                env = c.kwargs.get("env", {})
                if env:
                    assert env.get("PGPASSWORD") == "testpass"

    def test_no_pgpassword_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POSTGRES_PASSWORD")
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            db_restore("mydb", "/dump.pgsql")

            env = mock_run.call_args_list[0].kwargs.get("env", {})
            assert "PGPASSWORD" not in env

    def test_sets_pgport_when_postgres_port_env_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        with patch("lib.db.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            db_restore("mydb", "/dump.pgsql")

            env = mock_run.call_args_list[0].kwargs.get("env", {})
            assert env.get("PGPORT") == "5433"

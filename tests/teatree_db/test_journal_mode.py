import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.db.journal_mode import ControlDbJournal
from teatree.paths import CONTROL_DB_DIR_ENV


@pytest.mark.parametrize(
    ("location", "override", "expected", "header"),
    [("control", None, "wal", 2), ("host", None, "truncate", 1), ("control", "truncate", "truncate", 1)],
)
def test_shell_child_selects_mode_from_its_database_path(
    tmp_path: Path, location: str, override: str | None, expected: str, header: int
) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    db = (volume if location == "control" else tmp_path) / "db.sqlite3"
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("CREATE TABLE note(body TEXT)")
        connection.commit()

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "T3_CONFIG_DB": str(db),
        CONTROL_DB_DIR_ENV: str(volume),
        "TEATREE_ROLE": "worker",
        "DJANGO_SETTINGS_MODULE": "teatree.settings",
    }
    if override is not None:
        env["T3_CONTROL_DB_JOURNAL_MODE"] = override
    command = (
        "from django.db import connection; from pathlib import Path; import os; "
        "connection.settings_dict['NAME'] = os.environ['T3_CONFIG_DB']; "
        "c = connection.cursor(); c.execute('PRAGMA journal_mode'); "
        "print(c.fetchone()[0], Path(connection.settings_dict['NAME']).read_bytes()[18]); c.close()"
    )

    child = subprocess.run(
        [sys.executable, "-m", "teatree", "shell", "-c", command],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip().splitlines()[-1] == f"{expected} {header}"


def test_control_volume_selects_wal_and_preserves_other_init_commands(tmp_path: Path) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    journal = ControlDbJournal(volume / "db.sqlite3", {CONTROL_DB_DIR_ENV: str(volume)})

    assert journal.mode == "wal"
    assert journal.init_commands(["PRAGMA journal_mode=TRUNCATE", "PRAGMA foreign_keys=ON"]) == [
        "PRAGMA journal_mode=WAL",
        "PRAGMA foreign_keys=ON",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA journal_size_limit=67108864",
    ]


def test_reconnecting_does_not_repeat_wal_init_commands(tmp_path: Path) -> None:
    journal = ControlDbJournal(tmp_path / "db.sqlite3", {CONTROL_DB_DIR_ENV: str(tmp_path)}, containerized=True)
    initial = ["PRAGMA journal_mode=TRUNCATE", "PRAGMA foreign_keys=ON"]

    once = journal.init_commands(initial)

    assert journal.init_commands(once) == once


def test_host_path_and_override_select_truncate(tmp_path: Path) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    env = {CONTROL_DB_DIR_ENV: str(volume)}

    assert ControlDbJournal(tmp_path / "db.sqlite3", env).mode == "truncate"
    assert ControlDbJournal(volume / "db.sqlite3", {**env, "T3_CONTROL_DB_JOURNAL_MODE": "truncate"}).mode == "truncate"


def test_invalid_override_fails_before_open(tmp_path: Path) -> None:
    with pytest.raises(ImproperlyConfigured, match="T3_CONTROL_DB_JOURNAL_MODE"):
        assert ControlDbJournal(tmp_path / "db.sqlite3", {"T3_CONTROL_DB_JOURNAL_MODE": "delete"}).mode


def test_symlinked_and_relative_paths_stay_in_rollback_mode(tmp_path: Path) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    target = tmp_path / "outside.sqlite3"
    link = volume / "db.sqlite3"
    link.symlink_to(target)
    env = {CONTROL_DB_DIR_ENV: str(volume)}

    assert ControlDbJournal(link, env).mode == "truncate"
    assert ControlDbJournal(Path("db.sqlite3"), env).mode == "truncate"


def test_schema_qualified_and_mixed_case_journal_commands_are_replaced(tmp_path: Path) -> None:
    journal = ControlDbJournal(tmp_path / "db.sqlite3", {CONTROL_DB_DIR_ENV: str(tmp_path / "elsewhere")})

    assert journal.init_commands(
        [
            "pRaGmA main.JOURNAL_MODE = wal",
            "PRAGMA foreign_keys=ON",
            "PRAGMA journal_mode=WAL",
            "",
        ]
    ) == ["PRAGMA journal_mode=TRUNCATE", "PRAGMA foreign_keys=ON"]


def test_unreadable_mount_table_falls_back_to_truncate(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    with patch("teatree.db.write_domain.read_mount_entries", return_value=None):
        journal = ControlDbJournal(db, {CONTROL_DB_DIR_ENV: str(tmp_path)}, containerized=True)
        assert journal.mode == "truncate"
        assert "mount" in journal.reason


def test_wal_override_refuses_uncertain_control_db_without_changing_file(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("CREATE TABLE note(body TEXT)")
    before = db.read_bytes()[18]

    with patch("teatree.db.write_domain.read_mount_entries", return_value=None):
        journal = ControlDbJournal(
            db,
            {CONTROL_DB_DIR_ENV: str(tmp_path), "T3_CONTROL_DB_JOURNAL_MODE": " WAL "},
            containerized=True,
        )
        with pytest.raises(ImproperlyConfigured, match="mount"):
            _ = journal.mode
    assert db.read_bytes()[18] == before


def test_override_is_scoped_to_control_db_directory(tmp_path: Path) -> None:
    db = tmp_path / "isolated.sqlite3"
    journal = ControlDbJournal(
        db,
        {CONTROL_DB_DIR_ENV: str(tmp_path / "control"), "T3_CONTROL_DB_JOURNAL_MODE": "wal"},
        containerized=True,
    )
    assert journal.mode == "truncate"


def test_host_process_cannot_select_wal_for_control_db(tmp_path: Path) -> None:
    journal = ControlDbJournal(tmp_path / "db.sqlite3", {CONTROL_DB_DIR_ENV: str(tmp_path)}, containerized=False)
    assert journal.mode == "truncate"
    assert "container" in journal.reason


def test_blank_override_is_unset_and_values_are_normalized(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    env = {CONTROL_DB_DIR_ENV: str(tmp_path)}
    assert ControlDbJournal(db, {**env, "T3_CONTROL_DB_JOURNAL_MODE": "  TrUnCaTe  "}).mode == "truncate"
    with patch("teatree.db.write_domain.read_mount_entries", return_value=None):
        assert ControlDbJournal(db, {**env, "T3_CONTROL_DB_JOURNAL_MODE": " "}, containerized=True).mode == "truncate"

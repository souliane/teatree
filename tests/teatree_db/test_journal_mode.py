import os
import select
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.db import journal_mode
from teatree.db.journal_mode import ControlDbJournal
from teatree.paths import CONTROL_DB_DIR_ENV
from teatree.sqlite_snapshot import _sqlite_snapshot
from teatree.utils.mount_points import MountEntry

_BOUNDARY_CHILD_SETUP = """
import os
import django
from pathlib import Path
import teatree.db.boundary as boundary
import teatree.db.write_domain as write_domain
from teatree.utils.mount_points import MountEntry
boundary.is_running_in_container = lambda: True
write_domain.read_mount_entries = lambda: (MountEntry(Path('/'), 'ext4'),)
django.setup()
from django.db import connection
connection.settings_dict['NAME'] = os.environ['DB']
"""


def _control_child_env(db: Path) -> dict[str, str]:
    return {
        **os.environ,
        "DB": str(db),
        "T3_CONFIG_DB": str(db),
        CONTROL_DB_DIR_ENV: str(db.parent),
        "TEATREE_ROLE": "worker",
        "T3_CONTROL_DB_JOURNAL_MODE": "wal",
        "DJANGO_SETTINGS_MODULE": "teatree.settings",
    }


def _boundary_script(body: str) -> str:
    return f"{_BOUNDARY_CHILD_SETUP}\n{body}"


def _seed_through_boundary(db: Path, statements: str) -> None:
    child = subprocess.run(
        [sys.executable, "-c", _boundary_script(statements)],
        env=_control_child_env(db),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert db.read_bytes()[18] == 2


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
    env["T3_CONTROL_DB_JOURNAL_MODE"] = override or ("wal" if location == "control" else "truncate")
    command = (
        "from django.db import connection; from pathlib import Path; import os; "
        "import teatree.db.write_domain as wd; from teatree.utils.mount_points import MountEntry; "
        "wd.read_mount_entries=lambda: (MountEntry(Path('/'), 'ext4'),); "
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


def test_checkout_connection_preserves_existing_journal_until_rollout_override(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    with closing(sqlite3.connect(db)) as source:
        source.execute("CREATE TABLE note(body TEXT)")
        source.execute("INSERT INTO note VALUES ('before')")
        source.commit()
    env = _control_child_env(db)
    env.pop("T3_CONTROL_DB_JOURNAL_MODE")
    script = _boundary_script(
        "cursor=connection.cursor()\n"
        "cursor.execute('SELECT body FROM note')\n"
        "print(cursor.fetchone()[0], Path(os.environ['DB']).read_bytes()[18])\n"
        "connection.close()\n"
    )
    before_rollout = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False, timeout=30
    )
    assert before_rollout.returncode == 0, before_rollout.stderr
    assert before_rollout.stdout.strip() == "before 1"
    assert db.read_bytes()[18] == 1

    env["T3_CONTROL_DB_JOURNAL_MODE"] = "wal"
    rollout = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False, timeout=30
    )
    assert rollout.returncode == 0, rollout.stderr
    assert rollout.stdout.strip() == "before 2"
    assert db.read_bytes()[18] == 2

    env.pop("T3_CONTROL_DB_JOURNAL_MODE")
    after_rollout = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False, timeout=30
    )
    assert after_rollout.returncode == 0, after_rollout.stderr
    assert after_rollout.stdout.strip() == "before 2"


def test_checkout_refuses_unreadable_or_invalid_existing_header(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    db.write_bytes(b"invalid")
    journal = ControlDbJournal(db, {CONTROL_DB_DIR_ENV: str(tmp_path)}, containerized=True)
    with pytest.raises(ImproperlyConfigured, match="invalid existing control DB journal header"):
        _ = journal.mode
    with patch.object(Path, "open", side_effect=PermissionError("denied")):
        journal = ControlDbJournal(db, {CONTROL_DB_DIR_ENV: str(tmp_path)}, containerized=True)
        with pytest.raises(ImproperlyConfigured, match="cannot read the existing control DB journal header"):
            _ = journal.mode


def test_checkout_refuses_existing_wal_on_unverified_mount(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE note(body TEXT)")
    with patch("teatree.db.write_domain.read_mount_entries", return_value=None):
        journal = ControlDbJournal(db, {CONTROL_DB_DIR_ENV: str(tmp_path)}, containerized=True)
        with pytest.raises(ImproperlyConfigured, match="cannot safely open existing WAL database"):
            _ = journal.mode
    assert db.read_bytes()[18] == 2


def test_worktree_cannot_switch_installed_control_db_even_with_inherited_override(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("CREATE TABLE note(body TEXT)")
    env = {CONTROL_DB_DIR_ENV: str(tmp_path), "T3_CONTROL_DB_JOURNAL_MODE": "wal"}
    with patch("teatree.db.journal_mode.DEFAULT_CONTROL_DB_DIR", tmp_path):
        journal = ControlDbJournal(db, env, containerized=True)
        with pytest.raises(ImproperlyConfigured, match="worktree cannot change"):
            _ = journal.mode
    assert db.read_bytes()[18] == 1


def test_deployed_checkout_selects_wal_for_existing_local_db(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("CREATE TABLE note(body TEXT)")
    source_root = Path(__file__).resolve().parents[2]
    env = {CONTROL_DB_DIR_ENV: str(tmp_path), "TEATREE_CLONE_DIR": str(source_root)}
    with patch("teatree.db.write_domain.read_mount_entries", return_value=(MountEntry(Path("/"), "ext4"),)):
        assert ControlDbJournal(db, env, containerized=True).mode == "wal"


def test_source_copy_without_git_selects_wal_for_existing_local_db(tmp_path: Path) -> None:
    db = tmp_path / "control" / "db.sqlite3"
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("CREATE TABLE note(body TEXT)")
    source_copy = tmp_path / "source" / "src" / "teatree" / "db" / "journal_mode.py"
    source_copy.parent.mkdir(parents=True)
    shutil.copyfile(journal_mode.__file__, source_copy)
    env = {CONTROL_DB_DIR_ENV: str(db.parent)}

    with (
        patch.object(journal_mode, "__file__", str(source_copy)),
        patch("teatree.db.write_domain.read_mount_entries", return_value=(MountEntry(Path("/"), "ext4"),)),
    ):
        assert ControlDbJournal(db, env, containerized=True).mode == "wal"


def test_control_volume_selects_wal_and_preserves_other_init_commands(tmp_path: Path) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    with patch("teatree.db.write_domain.read_mount_entries", return_value=(MountEntry(Path("/"), "ext4"),)):
        journal = ControlDbJournal(volume / "db.sqlite3", {CONTROL_DB_DIR_ENV: str(volume)}, containerized=True)
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


def test_new_boundary_connection_restores_wal_journal_size_limit(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    _seed_through_boundary(db, "connection.cursor().execute('CREATE TABLE note(body TEXT)')\n")
    with closing(sqlite3.connect(db)) as raw:
        assert raw.execute("PRAGMA journal_size_limit=1024").fetchone() == (1024,)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _boundary_script(
                "cursor=connection.cursor()\ncursor.execute('PRAGMA journal_size_limit')\nprint(cursor.fetchone()[0])"
            ),
        ],
        env=_control_child_env(db),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "67108864"


@pytest.mark.parametrize(
    "setting", ["PRAGMA synchronous=FULL", 'PRAGMA "synchronous"=FULL', "PRAGMA 'synchronous'=FULL"]
)
def test_explicit_synchronous_setting_is_preserved(tmp_path: Path, setting: str) -> None:
    journal = ControlDbJournal(tmp_path / "db.sqlite3", {CONTROL_DB_DIR_ENV: str(tmp_path)}, containerized=True)
    with patch("teatree.db.write_domain.read_mount_entries") as read_mount:
        read_mount.return_value = (MountEntry(Path("/"), "ext4"),)
        commands = journal.init_commands(["PRAGMA journal_mode=TRUNCATE", setting])
    assert commands.count(setting) == 1
    assert "PRAGMA synchronous=NORMAL" not in commands


def test_selection_is_read_once_for_mode_reason_and_init_commands(tmp_path: Path) -> None:
    mounts = (MountEntry(Path("/"), "ext4"),)
    with patch("teatree.db.write_domain.read_mount_entries", return_value=mounts) as read_mount:
        journal = ControlDbJournal(tmp_path / "db.sqlite3", {CONTROL_DB_DIR_ENV: str(tmp_path)}, containerized=True)
        assert journal.mode == "wal"
        assert journal.reason == "control-DB volume"
        assert journal.init_commands([])[0] == "PRAGMA journal_mode=WAL"
        read_mount.assert_called_once_with()


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

    with patch("teatree.db.write_domain.read_mount_entries", return_value=(MountEntry(Path("/"), "ext4"),)):
        assert ControlDbJournal(link, env, containerized=True).mode == "truncate"
        assert ControlDbJournal(Path("db.sqlite3"), env, containerized=True).mode == "truncate"


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("unreadable", "mount table"),
        ("virtiofs", "not local"),
        ("nfs", "not local"),
        ("host", "outside the container"),
        ("relative", "relative or symlinked"),
        ("symlink", "relative or symlinked"),
    ],
)
def test_child_process_fails_closed_on_unsafe_control_db(tmp_path: Path, case: str, reason: str) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    db = volume / "db.sqlite3"
    if case == "relative":
        db = Path("db.sqlite3")
        volume = Path()
    if case == "symlink":
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        volume = link / "control"
        volume.mkdir()
        db = volume / "db.sqlite3"
    script = (
        "import os; import django; django.setup(); from pathlib import Path; from unittest.mock import patch; "
        "from django.db import connection; from teatree.db.journal_mode import ControlDbJournal; "
        "from teatree.utils.mount_points import MountEntry; "
        "case=os.environ['CASE']; entries=None if case=='unreadable' else (MountEntry(Path('/'), "
        "'nfs' if case=='nfs' else 'virtiofs' if case=='virtiofs' else 'ext4'),); "
        "p=patch('teatree.db.write_domain.read_mount_entries', return_value=entries); p.start(); "
        "patch('teatree.db.write_domain.is_running_in_container', return_value=case!='host').start(); "
        "patch('teatree.db.boundary.is_running_in_container', return_value=case!='host').start(); "
        "reason=ControlDbJournal(Path(os.environ['DB']), {os.environ['CONTROL_KEY']:os.environ['CONTROL_DIR']}, "
        "containerized=case!='host').reason; "
        "connection.settings_dict['NAME']=os.environ['DB']; "
        "c=connection.cursor(); c.execute('CREATE TABLE note(body TEXT)'); "
        "c.execute('PRAGMA journal_mode'); mode=c.fetchone()[0]; c.close(); connection.close(); "
        "print(mode, Path(os.environ['DB']).read_bytes()[18], reason)"
    )
    env = {
        **os.environ,
        "CASE": case,
        "DB": str(db),
        "T3_CONFIG_DB": str(tmp_path / "private.sqlite3"),
        "CONTROL_KEY": CONTROL_DB_DIR_ENV,
        "CONTROL_DIR": str(volume),
        CONTROL_DB_DIR_ENV: str(volume),
        "DJANGO_SETTINGS_MODULE": "teatree.settings",
        "TEATREE_ROLE": "worker" if case != "host" else "host",
    }
    env.pop("T3_CONTROL_DB_JOURNAL_MODE", None)
    child = subprocess.run(
        [sys.executable, "-c", script], env=env, cwd=tmp_path, capture_output=True, text=True, check=False, timeout=30
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.startswith("truncate 1 ")
    assert reason in child.stdout
    assert (db if db.is_absolute() else tmp_path / db).read_bytes()[18] == 1


def test_truncate_opener_waits_for_wal_writer_then_converts_file(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    writer = sqlite3.connect(db)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("CREATE TABLE note(body TEXT)")
        writer.execute("INSERT INTO note VALUES ('committed')")
        writer.commit()
        stock_script = (
            "import django, os; django.setup(); from django.db.utils import ConnectionHandler; "
            "h=ConnectionHandler({'default': {'ENGINE':'django.db.backends.sqlite3', 'NAME':os.environ['DB'], "
            "'OPTIONS':{'timeout':0.2, 'init_command':'PRAGMA journal_mode=TRUNCATE'}}}); "
            "h['default'].cursor()"
        )
        script = (
            "import django, os; django.setup(); from django.db.utils import ConnectionHandler; "
            "from teatree.settings import SQLITE_BOUNDARY_ENGINE, SQLITE_WRITE_SERIALIZATION_OPTIONS; "
            "h=ConnectionHandler({'default': {'ENGINE':SQLITE_BOUNDARY_ENGINE, 'NAME':os.environ['DB'], "
            "'OPTIONS':{**SQLITE_WRITE_SERIALIZATION_OPTIONS, 'timeout':0.2}}}); "
            "c=h['default'].cursor(); c.execute('PRAGMA journal_mode'); "
            "print(c.fetchone()[0]); c.close(); h.close_all()"
        )
        env = {
            **os.environ,
            "DB": str(db),
            CONTROL_DB_DIR_ENV: str(tmp_path),
            "T3_CONTROL_DB_JOURNAL_MODE": "truncate",
            "TEATREE_ROLE": "worker",
            "DJANGO_SETTINGS_MODULE": "teatree.settings",
        }
        stock_locked = subprocess.run(
            [sys.executable, "-c", stock_script], env=env, capture_output=True, text=True, check=False, timeout=30
        )
        assert stock_locked.returncode != 0
        assert "database is locked" in stock_locked.stderr.lower()
        locked = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False, timeout=30
        )
        assert locked.returncode != 0
        assert "database is locked" in locked.stderr.lower()
        assert db.read_bytes()[18] == 2
        assert writer.execute("SELECT body FROM note").fetchall() == [("committed",)]
        assert writer.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        writer.close()
    converted = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False, timeout=30
    )
    assert converted.returncode == 0, converted.stderr
    assert converted.stdout.strip() == "truncate"
    assert db.read_bytes()[18] == 1


def test_three_writer_processes_and_snapshot_reader_see_consistent_wal(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    _seed_through_boundary(
        db,
        "connection.cursor().execute('CREATE TABLE note(writer INT, sequence INT, UNIQUE(writer, sequence))')\n"
        "connection.cursor().execute('CREATE TABLE counter(value INT)')\n"
        "connection.cursor().execute('INSERT INTO counter VALUES (0)')\n",
    )
    script = _boundary_script("""
import sys, time
from django.db import transaction
connection.settings_dict['OPTIONS']['timeout'] = 2
cursor = connection.cursor()
assert cursor.execute('PRAGMA busy_timeout').fetchone() == (2000,)
cursor.execute('PRAGMA journal_mode')
assert cursor.fetchone() == ('wal',)
assert Path(os.environ['DB']).read_bytes()[18] == 2
cursor.execute('PRAGMA wal_autocheckpoint=0')
for sequence in range(50):
    with transaction.atomic():
        value = cursor.execute('SELECT value FROM counter').fetchone()[0]
        cursor.execute('UPDATE counter SET value=%s', (value + 1,))
        cursor.execute('INSERT INTO note VALUES (%s, %s)', (int(sys.argv[1]), sequence))
    time.sleep(.01)
connection.close()
""")
    snapshot_reader = sqlite3.connect(db, isolation_level=None)
    snapshot_reader.execute("BEGIN")
    assert snapshot_reader.execute("SELECT COUNT(*) FROM note").fetchone() == (0,)
    assert snapshot_reader.execute("SELECT value FROM counter").fetchone() == (0,)
    writers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(i)],
            env=_control_child_env(db),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(3)
    ]
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with closing(sqlite3.connect(db)) as reader:
                count = reader.execute("SELECT COUNT(*) FROM note").fetchone()[0]
            if count >= 3:
                break
            time.sleep(0.01)
        else:
            pytest.fail("writers did not commit before snapshot")
        assert db.read_bytes()[18] == 2
        assert Path(f"{db}-wal").stat().st_size > 0
        snapshot = tmp_path / "snapshot.sqlite3"
        _sqlite_snapshot(db, snapshot)
        assert snapshot.read_bytes()[18] == 1
        with closing(sqlite3.connect(snapshot)) as reader:
            snapshot_count = reader.execute("SELECT COUNT(*) FROM note").fetchone()[0]
            assert reader.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert 3 <= snapshot_count < 150
        assert snapshot_reader.execute("SELECT value FROM counter").fetchone() == (0,)
    finally:
        snapshot_reader.close()
        for writer in writers:
            try:
                _, stderr = writer.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                writer.kill()
                writer.communicate(timeout=5)
                raise
            assert writer.returncode == 0, stderr.decode()
    with closing(sqlite3.connect(db)) as reader:
        assert reader.execute("SELECT COUNT(*) FROM note").fetchone() == (150,)
        assert reader.execute("SELECT value FROM counter").fetchone() == (150,)


def test_four_processes_claim_one_wal_row_once(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    _seed_through_boundary(
        db,
        "connection.cursor().execute('CREATE TABLE claim(id INTEGER PRIMARY KEY, owner TEXT)')\n"
        "connection.cursor().execute('INSERT INTO claim VALUES (1, NULL)')\n",
    )
    script = _boundary_script("""
import sys, time
from django.db import transaction
connection.ensure_connection()
assert Path(os.environ['DB']).read_bytes()[18] == 2
with transaction.atomic():
    cursor = connection.cursor()
    row = cursor.execute('SELECT owner FROM claim WHERE id=1').fetchone()
    won = row[0] is None
    if won:
        cursor.execute('UPDATE claim SET owner=%s WHERE id=1 AND owner IS NULL', (sys.argv[1],))
        time.sleep(.05)
print('won' if won else 'lost')
connection.close()
""")
    claimers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(i)],
            env=_control_child_env(db),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(4)
    ]
    results = []
    for claimer in claimers:
        try:
            stdout, stderr = claimer.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            claimer.kill()
            claimer.communicate(timeout=5)
            raise
        assert claimer.returncode == 0, stderr.decode()
        results.append(stdout.decode().strip())
    assert sorted(results) == ["lost", "lost", "lost", "won"]
    with closing(sqlite3.connect(db)) as reader:
        assert reader.execute("SELECT owner FROM claim WHERE id=1").fetchone()[0] in {"0", "1", "2", "3"}
        assert reader.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_sigkilled_writer_leaves_committed_wal_row_recoverable(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    script = _boundary_script("""
import time
cursor = connection.cursor()
cursor.execute('PRAGMA journal_mode')
assert cursor.fetchone() == ('wal',)
cursor.execute('PRAGMA wal_autocheckpoint=0')
cursor.execute('CREATE TABLE note(body TEXT)')
cursor.execute("INSERT INTO note VALUES ('committed')")
print('ready', flush=True)
time.sleep(60)
""")
    writer = subprocess.Popen(
        [sys.executable, "-c", script],
        env=_control_child_env(db),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert writer.stdout is not None
        ready, _, _ = select.select([writer.stdout], [], [], 10)
        assert ready, "writer did not signal a committed row"
        assert writer.stdout.readline().strip() == b"ready"
        writer.kill()
        writer.communicate(timeout=5)
        assert db.read_bytes()[18] == 2
        assert Path(f"{db}-wal").stat().st_size > 0
        env = {**_control_child_env(db), "T3_CONTROL_DB_JOURNAL_MODE": "truncate"}
        converted = subprocess.run(
            [
                sys.executable,
                "-c",
                _boundary_script(
                    "cursor=connection.cursor()\n"
                    "cursor.execute('SELECT body FROM note')\n"
                    "print(cursor.fetchone()[0])\n"
                    "connection.close()\n"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert converted.returncode == 0, converted.stderr
        assert converted.stdout.strip() == "committed"
        assert db.read_bytes()[18] == 1
        assert not Path(f"{db}-wal").exists()
        with closing(sqlite3.connect(db)) as reader:
            assert reader.execute("SELECT body FROM note").fetchall() == [("committed",)]
            assert reader.execute("PRAGMA quick_check").fetchone() == ("ok",)
        snapshot = tmp_path / "snapshot.sqlite3"
        _sqlite_snapshot(db, snapshot)
        with closing(sqlite3.connect(snapshot)) as reader:
            assert reader.execute("SELECT body FROM note").fetchall() == [("committed",)]
    finally:
        if writer.poll() is None:
            writer.kill()
            writer.communicate(timeout=5)


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


@pytest.mark.parametrize(
    "command",
    [
        "PRAGMA journal_mode(WAL)",
        'PRAGMA "journal_mode"=WAL',
        "PRAGMA 'journal_mode'=WAL",
        "PRAGMA `journal_mode`=WAL",
        "PRAGMA [journal_mode]=WAL",
        'PRAGMA "main"."journal_mode"=WAL',
    ],
)
def test_all_valid_journal_pragmas_are_replaced(command: str, tmp_path: Path) -> None:
    journal = ControlDbJournal(tmp_path / "db.sqlite3", {CONTROL_DB_DIR_ENV: str(tmp_path / "elsewhere")})

    assert journal.init_commands([command, "PRAGMA foreign_keys=ON"]) == [
        "PRAGMA journal_mode=TRUNCATE",
        "PRAGMA foreign_keys=ON",
    ]


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
        connection.execute("INSERT INTO note VALUES ('intact')")
        connection.commit()
    before = db.read_bytes()[18]

    with patch("teatree.db.write_domain.read_mount_entries", return_value=None):
        journal = ControlDbJournal(
            db,
            {CONTROL_DB_DIR_ENV: str(tmp_path), "T3_CONTROL_DB_JOURNAL_MODE": " WAL "},
            containerized=True,
        )
        with pytest.raises(ImproperlyConfigured, match="mount"):
            _ = journal.mode
    refused = subprocess.run(
        [sys.executable, "-c", _boundary_script("write_domain.read_mount_entries=lambda: None\nconnection.cursor()\n")],
        env=_control_child_env(db),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert refused.returncode != 0
    assert "mount" in refused.stderr
    assert db.read_bytes()[18] == before
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT body FROM note").fetchall() == [("intact",)]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


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

# test-path: cross-cutting
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from teatree.utils.master_key import KeyFileSource
from teatree.utils.sealing import aad

READER = Path(__file__).resolve().parents[2] / "deploy" / "secret-read.sh"


def _fixture(tmp_path: Path, *, with_table: bool = True) -> tuple[Path, KeyFileSource]:
    db = tmp_path / "db.sqlite3"
    source = KeyFileSource(tmp_path / "master.key")
    with closing(sqlite3.connect(db)) as connection:
        if with_table:
            connection.execute(
                "CREATE TABLE teatree_sealed_secret (name TEXT PRIMARY KEY, key_id TEXT, nonce BLOB, ciphertext BLOB)"
            )
            connection.commit()
    return db, source


def _run(db: Path, source: KeyFileSource, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "teatree.config.cold_secret_reader", *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "T3_CONFIG_DB": str(db), "T3_MASTER_KEY_SOURCE": source.label},
    )


def test_cold_reader_distinguishes_value_absence_and_unreadable(tmp_path: Path) -> None:
    db, source = _fixture(tmp_path)
    name = "anthropic/a/oauth"
    sealed = source.create().seal("first-line\nsecond-line", aad=aad("instance", name))
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(
            "INSERT INTO teatree_sealed_secret VALUES (?, ?, ?, ?)",
            (name, sealed.key_id, sealed.nonce, sealed.ciphertext),
        )
        connection.commit()
    answered = _run(db, source, "get", name)
    assert (answered.returncode, answered.stdout) == (0, "first-line\n")
    assert _run(db, source, "get", "anthropic/absent").returncode == 1
    assert _run(db, source, "has-prefix", "anthropic/").returncode == 0
    assert _run(db, source, "probe-prefix", "anthropic/").returncode == 0
    source.path.unlink()
    unreadable = _run(db, source, "get", name)
    assert unreadable.returncode == 2
    assert "first-line" not in unreadable.stderr + unreadable.stdout


def test_pre_migration_table_is_absent(tmp_path: Path) -> None:
    db, source = _fixture(tmp_path, with_table=False)
    assert _run(db, source, "get", "team/token").returncode == 1


def test_unopenable_database_is_unreadable(tmp_path: Path) -> None:
    db = tmp_path / "directory-not-a-database"
    db.mkdir()
    source = KeyFileSource(tmp_path / "master.key")
    assert _run(db, source, "get", "team/token").returncode == 2


def test_shell_selector_reads_sealed_rows_without_invoking_pass(tmp_path: Path) -> None:
    db, source = _fixture(tmp_path)
    name = "team/token"
    sealed = source.create().seal("sealed-value\nmetadata", aad=aad("instance", name))
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(
            "INSERT INTO teatree_sealed_secret VALUES (?, ?, ?, ?)",
            (name, sealed.key_id, sealed.nonce, sealed.ciphertext),
        )
        connection.commit()
    log = tmp_path / "pass-calls"
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "pass"
    fake.write_text('#!/bin/sh\necho "$*" >> "$PASS_LOG"\nprintf "pass-value\\n"\n')
    fake.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "PASS_LOG": str(log),
        "T3_CONFIG_DB": str(db),
        "T3_MASTER_KEY_SOURCE": source.label,
        "T3_SECRET_STORE": "sealed",
        "T3_SECRET_PYTHON": sys.executable,
    }
    answered = subprocess.run([str(READER), "get", name], env=environment, capture_output=True, text=True, check=False)
    absent = subprocess.run(
        [str(READER), "get", "team/absent"], env=environment, capture_output=True, text=True, check=False
    )
    host = subprocess.run([str(READER), "host-get", name], env=environment, capture_output=True, text=True, check=False)
    assert (answered.returncode, answered.stdout) == (0, "sealed-value\n")
    assert (absent.returncode, host.returncode) == (1, 1)
    assert not log.exists()
    environment["T3_SECRET_STORE"] = "pass"
    from_pass = subprocess.run([str(READER), "get", name], env=environment, capture_output=True, text=True, check=False)
    assert (from_pass.returncode, from_pass.stdout) == (0, "pass-value\n")
    assert log.read_text().splitlines() == [f"show {name}"]


def test_deploy_shell_readers_use_the_one_selector_helper() -> None:
    root = READER.parent
    for name in ("t3", "deploy.sh", "roll.sh", "entrypoint.sh"):
        source = (root / name).read_text()
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert "secret-read.sh" in code
        assert "pass show" not in code

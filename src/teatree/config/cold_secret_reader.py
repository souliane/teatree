"""Read sealed rows before Django starts and from shell credential readers."""

import sqlite3
import sys
from pathlib import Path

from teatree.config.cold_db import canonical_config_db, read_readonly
from teatree.utils.master_key import KeyFileSource, master_key_source
from teatree.utils.sealing import SealedValue, aad
from teatree.utils.secrets import SecretStoreError

_ARG_COUNT = 2


def _database_missing(path: Path) -> bool:
    try:
        path.stat()
    except FileNotFoundError:
        return True
    return False


def _row(name: str, *, db: Path) -> tuple[str, bytes, bytes] | None:
    if _database_missing(db):
        return None

    def query(connection: sqlite3.Connection) -> tuple[str, bytes, bytes] | None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'teatree_sealed_secret'"
        ).fetchone()
        if exists is None:
            return None
        return connection.execute(
            "SELECT key_id, nonce, ciphertext FROM teatree_sealed_secret WHERE name = ?", (name,)
        ).fetchone()

    return read_readonly(db, query)


def read_sealed(name: str, *, db: Path | None = None, source: KeyFileSource | None = None) -> str | None:
    row = _row(name, db=db or canonical_config_db())
    if row is None:
        return None
    key_id, nonce, ciphertext = row
    key = (source or master_key_source()).load()
    return key.open(SealedValue(key_id, nonce, ciphertext), aad=aad("instance", name))


def first_name(prefix: str, *, db: Path | None = None) -> str | None:
    path = db or canonical_config_db()
    if _database_missing(path):
        return None

    def query(connection: sqlite3.Connection) -> str | None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'teatree_sealed_secret'"
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            "SELECT name FROM teatree_sealed_secret WHERE substr(name, 1, ?) = ? ORDER BY name LIMIT 1",
            (len(prefix), prefix),
        ).fetchone()
        return str(row[0]) if row is not None else None

    return read_readonly(path, query)


def _execute(operation: str, name: str) -> int:
    if operation == "get":
        value = read_sealed(name)
        if value is None:
            return 1
        first = value.split("\n", 1)[0]
        if not first:
            return 1
        sys.stdout.write(first + "\n")
        return 0
    candidate = first_name(name)
    if candidate is None:
        return 1
    if operation == "probe-prefix" and read_sealed(candidate) is None:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != _ARG_COUNT or args[0] not in {"get", "has-prefix", "probe-prefix"}:
        sys.stderr.write("usage: cold_secret_reader get|has-prefix|probe-prefix NAME\n")
        return 2
    operation, name = args
    try:
        return _execute(operation, name)
    except (OSError, sqlite3.Error, SecretStoreError, ValueError) as exc:
        sys.stderr.write(f"sealed secret {name!r} is unreadable: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

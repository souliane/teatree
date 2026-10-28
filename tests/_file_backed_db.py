"""Point the default connection at a file-backed copy of the test DB, so a worker thread sees the same rows.

Each xdist worker's ``:memory:`` connection is private, so a thread opening its own connection
would read an empty database. ``IMMEDIATE`` matches the production SQLite transaction mode.
"""

import sqlite3
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from django.db import connections


@contextmanager
def file_backed_default_database() -> Generator[Path]:
    wrapper = connections["default"]
    wrapper.ensure_connection()
    original_name = wrapper.settings_dict["NAME"]
    original_options = wrapper.settings_dict["OPTIONS"]
    original_transaction_mode = wrapper.transaction_mode
    original_raw = wrapper.connection
    if original_raw is None:
        msg = "the test database connection did not open"
        raise RuntimeError(msg)

    with TemporaryDirectory(prefix="t3-test-db-") as directory:
        source = Path(directory) / "source.sqlite3"
        destination = sqlite3.connect(source)
        try:
            original_raw.backup(destination)
        finally:
            destination.close()

        wrapper.connection = None
        wrapper.settings_dict["NAME"] = str(source)
        wrapper.settings_dict["OPTIONS"] = {**original_options, "timeout": 30, "transaction_mode": "IMMEDIATE"}
        wrapper.transaction_mode = "IMMEDIATE"
        try:
            yield source
        finally:
            redirected_raw = wrapper.connection
            if redirected_raw is not None:
                redirected_raw.close()
            wrapper.connection = None
            wrapper.settings_dict["NAME"] = original_name
            wrapper.settings_dict["OPTIONS"] = original_options
            wrapper.transaction_mode = original_transaction_mode
            wrapper.connection = original_raw


def hold_write_lock(database: Path, *, seconds: float) -> threading.Thread:
    """Take the SQLite write lock on a second connection; it is released after *seconds*. Join the thread to wait."""
    locked = threading.Event()

    def hold() -> None:
        raw = sqlite3.connect(database, timeout=0, isolation_level=None)
        raw.execute("BEGIN IMMEDIATE")
        locked.set()
        time.sleep(seconds)
        raw.execute("ROLLBACK")
        raw.close()

    holder = threading.Thread(target=hold)
    holder.start()
    assert locked.wait(5)
    return holder


@contextmanager
def writer_holding_the_lock(database: Path, *, seconds: float) -> Generator[None]:
    holder = hold_write_lock(database, seconds=seconds)
    try:
        yield
    finally:
        holder.join()

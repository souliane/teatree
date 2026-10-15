"""Point the default connection at a file-backed copy of the test DB, so a worker thread sees the same rows.

Each xdist worker's ``:memory:`` connection is private, so a thread opening its own connection
would read an empty database. ``IMMEDIATE`` matches the production SQLite transaction mode.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from django.db import connections


@contextmanager
def file_backed_default_database() -> Iterator[Path]:
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

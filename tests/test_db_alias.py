from pathlib import Path

import pytest
from django.db import connections

from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS
from tests.db_alias import register_control_db_alias, register_sqlite_alias, teardown_sqlite_alias, wal_control_db

pytestmark = pytest.mark.usefixtures(wal_control_db.__name__)


@pytest.mark.usefixtures("wal_control_db")
def test_control_alias_opens_in_wal(tmp_path: Path, django_db_blocker: pytest.FixtureRequest) -> None:
    alias = "test_control_alias_wal"
    try:
        with django_db_blocker.unblock():
            register_control_db_alias(alias, tmp_path / "db.sqlite3")
    finally:
        teardown_sqlite_alias(alias)


def test_serialization_options_refuse_stock_engine(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="boundary engine"):
        register_sqlite_alias("stock_invalid", tmp_path / "db.sqlite3", options=SQLITE_WRITE_SERIALIZATION_OPTIONS)
    assert "stock_invalid" not in connections.databases

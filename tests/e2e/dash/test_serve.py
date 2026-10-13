"""Checkout server guards protect the live configuration database."""

import sqlite3
from pathlib import Path

import pytest

import teatree
from e2e.dash import serve


def test_foreign_import_refusal_names_both_locations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = tmp_path / "other" / "teatree" / "__init__.py"
    monkeypatch.setattr(teatree, "__file__", str(foreign))

    with pytest.raises(SystemExit) as error:
        serve.main()

    assert str(foreign.parent) in str(error.value)
    assert str(Path(serve.__file__).resolve().parents[2] / "src" / "teatree") in str(error.value)


@pytest.mark.parametrize("settings_module", ["teatree.settings", "e2e.dash.settings"])
def test_live_config_database_is_byte_identical_after_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings_module: str
) -> None:
    live_db = tmp_path / "live.sqlite3"
    connection = sqlite3.connect(live_db)
    try:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('untouched')")
        connection.commit()
    finally:
        connection.close()
    before = live_db.read_bytes()
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", settings_module)
    monkeypatch.setenv("T3_CONFIG_DB", str(live_db))

    with pytest.raises(SystemExit):
        serve.main()

    assert live_db.read_bytes() == before

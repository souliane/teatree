"""Checkout server guards protect the live configuration database."""

import sqlite3
from pathlib import Path

import pytest

import teatree
from e2e.dash import serve

DATABASE_OVERRIDES = ("T3_CONFIG_DB", "T3_CONTROL_DB_DIR", "DATABASE_URL")
SCRATCH_REFUSAL = "visual QA requires an isolated scratch directory"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in (*DATABASE_OVERRIDES, "XDG_DATA_HOME", "TMPDIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "e2e.dash.settings")
    return monkeypatch


def test_foreign_import_refusal_names_both_locations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = tmp_path / "other" / "teatree" / "__init__.py"
    monkeypatch.setattr(teatree, "__file__", str(foreign))

    with pytest.raises(SystemExit) as error:
        serve.main()

    assert str(foreign.parent) in str(error.value)
    assert str(Path(serve.__file__).resolve().parents[2] / "src" / "teatree") in str(error.value)


@pytest.mark.parametrize(
    ("variable", "settings_module", "refusal"),
    [
        ("T3_CONFIG_DB", "teatree.settings", "visual QA requires e2e.dash.settings"),
        ("T3_CONFIG_DB", "e2e.dash.settings", "visual QA refuses inherited database overrides"),
        ("T3_CONTROL_DB_DIR", "e2e.dash.settings", "visual QA refuses inherited database overrides"),
        ("DATABASE_URL", "e2e.dash.settings", "visual QA refuses inherited database overrides"),
    ],
)
def test_live_database_override_is_refused_and_left_untouched(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, variable: str, settings_module: str, refusal: str
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
    clean_env.setenv("DJANGO_SETTINGS_MODULE", settings_module)
    clean_env.setenv(variable, str(live_db))

    with pytest.raises(SystemExit, match=refusal):
        serve.main()

    assert live_db.read_bytes() == before


@pytest.mark.parametrize(
    ("xdg_data_home", "tmpdir"),
    [
        ("teatree-visual-qa-xdg", "teatree-visual-qa-tmp"),
        ("scratch", "scratch"),
    ],
    ids=["tmpdir-differs", "not-a-visual-qa-directory"],
)
def test_scratch_directory_must_be_one_the_gate_created(
    tmp_path: Path, clean_env: pytest.MonkeyPatch, xdg_data_home: str, tmpdir: str
) -> None:
    clean_env.setenv("XDG_DATA_HOME", str(tmp_path / xdg_data_home))
    clean_env.setenv("TMPDIR", str(tmp_path / tmpdir))

    with pytest.raises(SystemExit, match=SCRATCH_REFUSAL):
        serve.main()

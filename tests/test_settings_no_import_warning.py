"""Importing teatree settings must not emit a warning (filterwarnings=error).

A legacy/stale ``db.sqlite3`` under DATA_DIR is an operational nudge that
``t3 doctor check`` surfaces -- not a Python warning at settings import. The
suite imports settings on every collection under ``filterwarnings = ["error"]``,
so a settings-level ``warnings.warn`` turned a benign stale DB into a hard
collection error and forced ``SKIP=pytest`` / ``--no-verify``. This pins that
the import path stays warning-free regardless of on-disk stale DBs.
"""

import importlib
import importlib.resources
import warnings

import teatree.settings as settings_module
from teatree import paths


def test_settings_import_emits_no_warning_with_stale_db(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "db.sqlite3").touch()
    stale = data_dir / "teatree" / "db.sqlite3"
    stale.parent.mkdir()
    stale.touch()

    monkeypatch.setattr(paths, "DATA_DIR", data_dir)
    monkeypatch.setattr(paths, "DATA_DIR_AUTO_ISOLATED", False)
    monkeypatch.setattr(paths, "CANONICAL_DB", data_dir / "db.sqlite3")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        importlib.reload(settings_module)


def test_settings_module_does_not_call_warnings_warn() -> None:
    source = importlib.resources.files("teatree").joinpath("settings.py").read_text()
    assert "warnings.warn" not in source, (
        "teatree/settings.py must not warn at import -- the stale-DB notice lives "
        "in `t3 doctor check`, or filterwarnings=error breaks pytest collection"
    )

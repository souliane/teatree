"""Importing teatree settings must not emit a warning (filterwarnings=error).

A legacy/stale ``db.sqlite3`` under DATA_DIR is an operational nudge that
``t3 doctor check`` surfaces -- not a Python warning at settings import. The
suite imports settings on every collection under ``filterwarnings = ["error"]``,
so a settings-level ``warnings.warn`` turned a benign stale DB into a hard
collection error and forced ``SKIP=pytest`` / ``--no-verify``. This pins that
the import path stays warning-free regardless of on-disk stale DBs.
"""

import importlib.resources
import os
import subprocess
import sys


def test_settings_import_emits_no_warning_with_stale_db(tmp_path) -> None:
    """A stale on-disk DB must not make ``import teatree.settings`` warn.

    Runs the import in a fresh subprocess under ``-W error`` so the
    in-process ``sys.modules`` (and the shared
    ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` / ``DATABASES`` objects other
    tests assert identity against) is never reloaded or swapped. The
    subprocess points DATA_DIR at a sandbox via ``XDG_DATA_HOME`` and seeds
    a non-canonical ``db.sqlite3`` so a re-added import-time
    ``warnings.warn`` would surface as a stale-DB warning and fail the
    import -- the test stays non-vacuous.
    """
    xdg = tmp_path / "xdg"
    data_dir = xdg / "teatree"
    (data_dir / "legacy").mkdir(parents=True)
    (data_dir / "db.sqlite3").touch()  # canonical
    (data_dir / "legacy" / "db.sqlite3").touch()  # stale, non-canonical

    env = dict(os.environ)
    env["XDG_DATA_HOME"] = str(xdg)
    proc = subprocess.run(
        [sys.executable, "-W", "error", "-c", "import teatree.settings"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        "importing teatree.settings with a stale DB present emitted a warning "
        f"(or failed) under -W error:\n{proc.stderr}"
    )


def test_settings_module_does_not_call_warnings_warn() -> None:
    source = importlib.resources.files("teatree").joinpath("settings.py").read_text()
    assert "warnings.warn" not in source, (
        "teatree/settings.py must not warn at import -- the stale-DB notice lives "
        "in `t3 doctor check`, or filterwarnings=error breaks pytest collection"
    )

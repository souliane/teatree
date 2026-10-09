"""Shared staging helpers for the teatree config test package.

``_write_manage_py`` stages an on-disk ``manage.py`` (overlay-class discovery);
``_seed_config_db`` builds a real ``teatree_config_setting`` sqlite the Django-free
``cold_reader`` resolves via ``T3_CONFIG_DB`` (the DB-home config store).
"""

import json
import sqlite3
from pathlib import Path


def _write_manage_py(project_path: Path, settings_module: str = "myapp.settings") -> None:
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "manage.py").write_text(f'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{settings_module}")\n')


def _seed_config_db(db_path: Path, *, scope: str = "", **rows: object) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
                (scope, key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()

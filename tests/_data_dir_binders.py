"""Src modules that capture the data directory at import, and how each is isolated per test.

``tests/conftest.py`` applies :data:`DATA_DIR_BINDERS` in its autouse environment fixture and
``tests/conformance/test_data_dir_binders_isolated.py`` fails on a binder missing from here.
"""

from pathlib import Path

import pytest

#: ``"<module>:<name>"`` -> the path under the per-test data directory the name is rebound to ("" is the directory).
DATA_DIR_BINDERS: dict[str, str] = {
    "teatree.backends.slack.listener_heartbeat:DATA_DIR": "",
    "teatree.cli.config_view:CANONICAL_DB": "db.sqlite3",
    "teatree.cli.config_view:DATA_DIR": "",
    "teatree.cli.doctor.checks_db_integrity:DATA_DIR": "",
    "teatree.cli.mcp_owning_domain:CANONICAL_DB": "db.sqlite3",
    "teatree.config:DATA_DIR": "",
    "teatree.core.checkpoint:DATA_DIR": "",
    "teatree.core.skill_cache:DATA_DIR": "",
    "teatree.loops.worker_health:DATA_DIR": "",
    "teatree.repo_mode:DATA_DIR": "",
    "teatree.update_check:DATA_DIR": "",
    "teatree.utils.bad_artifacts:DATA_DIR": "",
    "teatree.utils.bad_artifacts:_CACHE_FILE": "bad_artifacts.json",
    "teatree.utils.singleton:CANONICAL_DB": "db.sqlite3",
    "teatree.utils.singleton:DATA_DIR": "",
    "teatree.utils.singleton_refusals:DATA_DIR": "",
    "teatree.utils.worker_spawn:DATA_DIR": "",
    "teatree.utils.worker_spawn:SPAWN_LOG_PATH": "worker-spawn.log",
}

#: Whole modules left alone, each with the reason a per-test rebinding would be wrong.
EXEMPT_MODULES: dict[str, str] = {
    "teatree.settings": (
        "evaluated once at process start, before any fixture runs; the test database is swapped by "
        "django_db_setup, and rebinding settings constants mid-run would not reach Django's loaded settings"
    ),
}


def isolate_data_dir_binders(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    for binder, relative in DATA_DIR_BINDERS.items():
        module, name = binder.split(":", 1)
        monkeypatch.setattr(f"{module}.{name}", data_dir / relative if relative else data_dir)

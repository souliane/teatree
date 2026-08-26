"""Shared staging helpers for the t3 doctor test package.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). The home-sandbox stager, the DB-home ``overlays``
registry seeder (the legacy file tier is removed — the registry is read via
the Django-free ``cold_reader`` at ``T3_CONFIG_DB``), and the fake-entry-point
/ editable-map builders every focused doctor test relies on, relocated so each
split module imports them instead of redefining them.
"""

import json
import sqlite3
from pathlib import Path


def _seed_overlays(tmp_path: Path, monkeypatch, overlays: dict[str, object]) -> Path:
    """Seed the DB-home ``overlays`` registry in a cold sqlite config DB.

    Overlay discovery reads the registry through the Django-free ``cold_reader``
    at ``T3_CONFIG_DB``, so an overlay-shaped doctor test stages the overlay here.
    """
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'overlays', ?)",
            (json.dumps(overlays),),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    return db


def _stage_home(tmp_path: Path, monkeypatch) -> Path:
    """Isolate overlay discovery under ``tmp_path``.

    - Redirects ``Path.home()`` to ``tmp_path`` so ``~/.claude/...`` lookups are sandboxed.
    - Muzzles ``importlib.metadata.entry_points`` so installed overlays (``t3-teatree``)
        don't leak into ``discover_overlays()`` / ``discover_active_overlay()``.
    - Moves cwd under ``tmp_path`` so ``_discover_from_manage_py`` cannot climb into
        the real teatree checkout.
    - Points ``T3_REPO`` at a staged checkout so the shard-durations checks judge a
        controlled tree. A neutral cwd is not enough: repo resolution falls through
        to ``T3_REPO`` and then to the installed editable source, so a staged run
        judged the developer's own ``dev/.test_durations`` and failed on its real
        coverage. Both checks run for REAL against the staged tree — stubbing the
        measurements out instead is how a check quietly stops being exercised here.
    - Points ``T3_CONTROL_DB_DIR`` at a staged readable directory. The control-DB
        check reads the venue otherwise: inside a container it treats the default
        volume path as in use, so a test runner with no volume mounted turns a whole
        `doctor check` red on a fact about the runner. Staged, the check still runs
        for real — against a directory this test owns.
    """
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
    monkeypatch.setenv("T3_REPO", str(_stage_healthy_shard_durations(tmp_path)))
    monkeypatch.setenv("T3_CONTROL_DB_DIR", str(_stage_control_db_dir(tmp_path)))
    neutral = tmp_path / "_neutral_cwd"
    neutral.mkdir(exist_ok=True)
    monkeypatch.chdir(neutral)
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    return tmp_path


def _stage_control_db_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "_staged_control_db"
    directory.mkdir(exist_ok=True)
    return directory


#: One recorded test, comfortably inside the lane ceiling: full file coverage, no
#: orphan key, no ceiling pressure — the healthy fixture both shard-durations
#: checks are aggregated against by the ``t3 doctor check`` command tests.
STAGED_DURATIONS_NODE_ID = "tests/test_staged.py::test_staged"


def _stage_healthy_shard_durations(tmp_path: Path) -> Path:
    repo = tmp_path / "_staged_repo"
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "dev").mkdir(parents=True, exist_ok=True)
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntimeout = 60\n", encoding="utf-8")
    (repo / "tests" / "test_staged.py").write_text("def test_staged() -> None:\n    pass\n", encoding="utf-8")
    (repo / "dev" / ".test_durations").write_text(json.dumps({STAGED_DURATIONS_NODE_ID: 1.0}), encoding="utf-8")
    return repo


def _fake_entry_point(dist_name: str = "my-overlay") -> object:
    """Return a fake ``importlib.metadata.EntryPoint`` with ``dist.name``.

    A real ``EntryPoint`` also carries a ``value`` (the overlay class path),
    which overlay discovery reads (``discover_overlays``); the fake provides a
    plausible one so the entry-point branch resolves without an attribute error.
    """
    dist = type("_FakeDist", (), {"name": dist_name})()
    return type(
        "_FakeEP",
        (),
        {"name": f"t3-{dist_name}", "value": f"{dist_name}.overlay:Overlay", "dist": dist},
    )()


def _editable_map(**dists: tuple[bool, str]):
    """Build an ``editable_info`` side_effect from a ``dist_name -> (editable, url)`` map."""

    def side_effect(dist_name: str) -> tuple[bool, str]:
        return dists.get(dist_name, (False, ""))

    return side_effect

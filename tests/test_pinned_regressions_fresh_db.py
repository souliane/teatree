# test-path: cross-cutting — the pinned-regressions lane (teatree.cli.eval.lanes) against an ambient host DB.
"""The pinned-regressions lane migrates its OWN fresh DB, never the ambient host one.

Before: the lane ran its ORM checks against the runtime-resolved self-DB, so any
host DB whose migration state disagreed with the code (a pre-squash snapshot, a
stale worktree copy) reddened the pre-push gate with `OperationalError` the diff
did not cause — red on unmodified main too.
"""

import contextlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _lane_against_stale_ambient_db(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    data_dir = tmp_path / "teatree"
    data_dir.mkdir()
    with contextlib.closing(sqlite3.connect(data_dir / "db.sqlite3")) as stale:
        stale.execute("CREATE TABLE teatree_anthropic_token_usage (x)")
        stale.commit()
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path)}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    return subprocess.run(
        [str(Path(sys.executable).with_name("t3")), "eval", "pinned-regressions"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_a_stale_ambient_db_does_not_red_the_lane(tmp_path: Path) -> None:
    result = _lane_against_stale_ambient_db(tmp_path)
    assert [line for line in result.stdout.splitlines() if line.startswith("FAIL")] == []
    assert result.returncode == 0, result.stdout[-2000:]


def test_the_ambient_db_is_left_untouched(tmp_path: Path) -> None:
    _lane_against_stale_ambient_db(tmp_path)
    with contextlib.closing(sqlite3.connect(tmp_path / "teatree" / "db.sqlite3")) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"teatree_anthropic_token_usage"}

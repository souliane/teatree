# test-path: cross-cutting — run_regression_corpus, shared by `t3 eval pinned-regressions` and the `t3 eval` suite.
"""The regression corpus runs on its own fresh DB, never the ambient host one."""

import contextlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

_CORPUS = (
    "import sys\n"
    "from teatree.utils.django_bootstrap import ensure_django\n"
    "ensure_django()\n"
    "from teatree.eval.regression_corpus import render_text, run_regression_corpus\n"
    "report = run_regression_corpus()\n"
    "print(render_text(report))\n"
    "sys.exit(0 if report.ok else 1)\n"
)


def test_a_stale_ambient_db_neither_reds_the_corpus_nor_is_touched(tmp_path: Path) -> None:
    ambient = tmp_path / "teatree" / "db.sqlite3"
    ambient.parent.mkdir()
    with contextlib.closing(sqlite3.connect(ambient)) as stale:
        stale.execute("CREATE TABLE teatree_anthropic_token_usage (x)")
        stale.commit()
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path)}
    for leaked in ("DJANGO_SETTINGS_MODULE", "T3_REPO", "T3_CONFIG_DB"):
        env.pop(leaked, None)

    result = subprocess.run(
        [sys.executable, "-c", _CORPUS],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert [line for line in result.stdout.splitlines() if line.startswith("FAIL")] == []
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    with contextlib.closing(sqlite3.connect(ambient)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"teatree_anthropic_token_usage"}

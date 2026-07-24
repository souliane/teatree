"""The whole-tree module-health DEBT report (souliane/teatree#3511).

The commit-time ratchet grandfathers over-cap files, so the standing debt stays
invisible until an unrelated PR inherits a split mid-task. ``run_debt_report``
makes the same set visible on demand — advisory, never blocking. Driven against a
controlled ``src/`` tree under ``tmp_path`` so the assertion is deterministic.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from teatree.hooks.portable.check_module_health import MAX_LOC, run_debt_report


def _seed_src(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "teatree"
    src.mkdir(parents=True)
    return src


def test_debt_report_names_an_over_cap_module_and_never_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _seed_src(tmp_path)
    (src / "huge.py").write_text("\n".join(f"a_{i} = {i}" for i in range(MAX_LOC + 50)) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_debt_report()

    out = buf.getvalue()
    assert rc == 0, "the debt report is advisory — it must never block"
    assert "huge.py" in out
    assert f"cap {MAX_LOC}" in out


def test_debt_report_says_none_on_a_clean_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _seed_src(tmp_path)
    (src / "small.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_debt_report()

    assert rc == 0
    assert "none" in buf.getvalue()

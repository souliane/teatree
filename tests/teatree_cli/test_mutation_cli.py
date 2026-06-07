"""CLI surface for ``t3 mutation run`` — the programmatic baseline ratchet.

The runner's mutmut half is exercised by ``tests/quality/test_mutation_kill_proof.py``;
this file pins the CLI's verdict and ``--update-baseline`` wiring with the scoped
run stubbed, so it is fast and deterministic.
"""

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli import mutation as mutation_cli
from teatree.quality.mutation_run import MutationOutcome

runner = CliRunner()


def _pyproject(tmp_path: Path, *, baseline: list[tuple[str, int]]) -> Path:
    path = tmp_path / "pyproject.toml"
    entries = ", ".join(f'{{ path = "{p}", count = {c} }}' for p, c in baseline)
    path.write_text(
        "[tool.teatree.mutation]\n"
        'mode = "warn"\n'
        'high_value_modules = [ "src/teatree/a.py", "src/teatree/b.py" ]\n'
        f"baseline_surviving = [ {entries} ]\n",
        encoding="utf-8",
    )
    return path


def _stub_run(monkeypatch: pytest.MonkeyPatch, outcome: MutationOutcome) -> None:
    monkeypatch.setattr(mutation_cli, "run_scoped", lambda **_kwargs: outcome)


def _point_at(monkeypatch: pytest.MonkeyPatch, pyproject: Path) -> None:
    monkeypatch.setattr(mutation_cli, "registry_pyproject_path", lambda: pyproject)
    monkeypatch.setattr("teatree.quality.mutation_run.registry_pyproject_path", lambda: pyproject)


class TestRunVerdict:
    def test_exits_nonzero_when_survivors_exceed_baseline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _point_at(monkeypatch, _pyproject(tmp_path, baseline=[("src/teatree/a.py", 1)]))
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/a.py",),
            survived=("teatree.a.f__mutmut_1", "teatree.a.g__mutmut_2"),
            killed=(),
            inconclusive=(),
        )
        _stub_run(monkeypatch, outcome)
        result = runner.invoke(app, ["mutation", "run"])
        assert result.exit_code == 1

    def test_exits_zero_at_or_below_baseline(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _point_at(monkeypatch, _pyproject(tmp_path, baseline=[("src/teatree/a.py", 2)]))
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/a.py",),
            survived=("teatree.a.f__mutmut_1", "teatree.a.g__mutmut_2"),
            killed=(),
            inconclusive=(),
        )
        _stub_run(monkeypatch, outcome)
        result = runner.invoke(app, ["mutation", "run"])
        assert result.exit_code == 0

    def test_no_op_exits_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _point_at(monkeypatch, _pyproject(tmp_path, baseline=[]))
        _stub_run(monkeypatch, MutationOutcome(scoped_modules=(), survived=(), killed=(), inconclusive=()))
        result = runner.invoke(app, ["mutation", "run"])
        assert result.exit_code == 0

    def test_reports_below_baseline_without_failing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _point_at(monkeypatch, _pyproject(tmp_path, baseline=[("src/teatree/a.py", 5)]))
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/a.py",),
            survived=("teatree.a.f__mutmut_1",),
            killed=(),
            inconclusive=(),
        )
        _stub_run(monkeypatch, outcome)
        result = runner.invoke(app, ["mutation", "run"])
        assert result.exit_code == 0
        assert "below the baseline" in result.output


class TestUpdateBaseline:
    def test_ratchets_the_pyproject_count_down(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pyproject = _pyproject(tmp_path, baseline=[("src/teatree/a.py", 4)])
        _point_at(monkeypatch, pyproject)
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/a.py",),
            survived=("teatree.a.f__mutmut_1",),
            killed=(),
            inconclusive=(),
        )
        _stub_run(monkeypatch, outcome)
        result = runner.invoke(app, ["mutation", "run", "--all", "--update-baseline"])
        assert result.exit_code == 0
        written = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        counts = {e["path"]: e["count"] for e in written["tool"]["teatree"]["mutation"]["baseline_surviving"]}
        assert counts == {"src/teatree/a.py": 1}

    def test_refuses_to_loosen_without_allow_regression(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pyproject = _pyproject(tmp_path, baseline=[("src/teatree/a.py", 1)])
        _point_at(monkeypatch, pyproject)
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/a.py",),
            survived=("teatree.a.f__mutmut_1", "teatree.a.g__mutmut_2", "teatree.a.h__mutmut_3"),
            killed=(),
            inconclusive=(),
        )
        _stub_run(monkeypatch, outcome)
        result = runner.invoke(app, ["mutation", "run", "--all", "--update-baseline"])
        assert result.exit_code == 1
        unchanged = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        counts = {e["path"]: e["count"] for e in unchanged["tool"]["teatree"]["mutation"]["baseline_surviving"]}
        assert counts == {"src/teatree/a.py": 1}

    def test_ratchet_to_zero_drops_the_entry(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pyproject = _pyproject(tmp_path, baseline=[("src/teatree/a.py", 3)])
        _point_at(monkeypatch, pyproject)
        outcome = MutationOutcome(scoped_modules=("src/teatree/a.py",), survived=(), killed=(), inconclusive=())
        _stub_run(monkeypatch, outcome)
        result = runner.invoke(app, ["mutation", "run", "--all", "--update-baseline"])
        assert result.exit_code == 0
        written = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        assert written["tool"]["teatree"]["mutation"]["baseline_surviving"] == []

    def test_no_op_run_reports_nothing_to_rebaseline(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pyproject = _pyproject(tmp_path, baseline=[("src/teatree/a.py", 3)])
        _point_at(monkeypatch, pyproject)
        _stub_run(monkeypatch, MutationOutcome(scoped_modules=(), survived=(), killed=(), inconclusive=()))
        result = runner.invoke(app, ["mutation", "run", "--update-baseline"])
        assert result.exit_code == 0
        assert "nothing to re-baseline" in result.output
        unchanged = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        counts = {e["path"]: e["count"] for e in unchanged["tool"]["teatree"]["mutation"]["baseline_surviving"]}
        assert counts == {"src/teatree/a.py": 3}

    def test_allow_regression_records_the_higher_count(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pyproject = _pyproject(tmp_path, baseline=[("src/teatree/a.py", 1)])
        _point_at(monkeypatch, pyproject)
        outcome = MutationOutcome(
            scoped_modules=("src/teatree/a.py",),
            survived=("teatree.a.f__mutmut_1", "teatree.a.g__mutmut_2", "teatree.a.h__mutmut_3"),
            killed=(),
            inconclusive=(),
        )
        _stub_run(monkeypatch, outcome)
        result = runner.invoke(app, ["mutation", "run", "--all", "--update-baseline", "--allow-regression"])
        assert result.exit_code == 0
        written = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        counts = {e["path"]: e["count"] for e in written["tool"]["teatree"]["mutation"]["baseline_surviving"]}
        assert counts == {"src/teatree/a.py": 3}

"""The publish-path guard that refuses an unmetered (OAuth-exhausted) shard.

Every fixture is a REAL ``render_matrix_html`` artifact written to ``tmp_path``,
so the guard is exercised against the exact bytes the weekly workflow publishes.
"""

from pathlib import Path

import pytest

from teatree.eval.benchmark_publish_guard import UnmeteredShardError, contaminated_shards, verify_publishable
from teatree.eval.matrix import MatrixRow, render_matrix_html
from teatree.eval.models import EvalSpec


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        judge=None,
    )


def _row(scenario: str, model: str, *, passed: bool, cost_usd: float) -> MatrixRow:
    return MatrixRow(
        scenario=scenario,
        model=model,
        passed=passed,
        score=1.0 if passed else 0.0,
        trials=1,
        skipped=False,
        cost_usd=cost_usd,
    )


def _write_shard(directory: Path, name: str, rows: list[MatrixRow], models: list[str]) -> Path:
    specs = [_spec(scenario) for scenario in dict.fromkeys(row.scenario for row in rows)]
    path = directory / f"eval-benchmark-{name}.html"
    path.write_text(render_matrix_html(rows, models, specs), encoding="utf-8")
    return path


def _metered_rows(model: str, *, cost_usd: float = 0.1581) -> list[MatrixRow]:
    return [
        _row("alpha", model, passed=True, cost_usd=cost_usd),
        _row("beta", model, passed=False, cost_usd=cost_usd),
        _row("gamma", model, passed=True, cost_usd=cost_usd),
    ]


def _unmetered_rows(model: str) -> list[MatrixRow]:
    """The OAuth-exhausted signature: every scenario force-FAILs at zero cost."""
    return [_row(scenario, model, passed=False, cost_usd=0.0) for scenario in ("alpha", "beta", "gamma")]


class TestContaminationDetection:
    def test_all_models_zero_cost_is_refused(self, tmp_path: Path) -> None:
        models = ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"]
        rows = [row for model in models for row in _unmetered_rows(model)]
        _write_shard(tmp_path, "under_load-2-5", rows, models)

        with pytest.raises(UnmeteredShardError) as excinfo:
            verify_publishable(tmp_path)

        message = str(excinfo.value)
        assert "under_load-2-5" in message
        assert all(model in message for model in models)

    def test_partial_contamination_is_refused(self, tmp_path: Path) -> None:
        """One model metered, two at zero cost with verdicts — run 29760291395's shard 3-5."""
        models = ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"]
        rows = [
            *_metered_rows("claude-opus-4-8", cost_usd=0.1582),
            *_unmetered_rows("claude-sonnet-5"),
            *_unmetered_rows("claude-haiku-4-5"),
        ]
        _write_shard(tmp_path, "under_load-3-5", rows, models)

        with pytest.raises(UnmeteredShardError) as excinfo:
            verify_publishable(tmp_path)

        message = str(excinfo.value)
        assert "under_load-3-5" in message
        assert "claude-sonnet-5" in message
        assert "claude-haiku-4-5" in message
        assert "claude-opus-4-8" not in message

    def test_fully_metered_shard_is_published(self, tmp_path: Path) -> None:
        models = ["claude-opus-4-8", "claude-sonnet-5"]
        rows = [row for model in models for row in _metered_rows(model)]
        shard = _write_shard(tmp_path, "clean_room-1-16", rows, models)
        before = shard.read_text(encoding="utf-8")

        verify_publishable(tmp_path)

        assert contaminated_shards(tmp_path) == []
        assert shard.read_text(encoding="utf-8") == before

    def test_shard_with_no_scenarios_is_not_contaminated(self, tmp_path: Path) -> None:
        models = ["claude-opus-4-8", "claude-sonnet-5"]
        _write_shard(tmp_path, "under_load-5-5", [], models)

        verify_publishable(tmp_path)

    def test_all_skipped_shard_is_not_contaminated(self, tmp_path: Path) -> None:
        """No verdicts recorded means no metered work was owed."""
        rows = [
            MatrixRow(
                scenario=scenario,
                model="claude-opus-4-8",
                passed=False,
                score=0.0,
                trials=1,
                skipped=True,
            )
            for scenario in ("alpha", "beta")
        ]
        _write_shard(tmp_path, "clean_room-2-16", rows, ["claude-opus-4-8"])

        verify_publishable(tmp_path)

    def test_one_bad_shard_refuses_the_whole_publish(self, tmp_path: Path) -> None:
        """A partially-published dashboard that looks complete is the failure mode."""
        models = ["claude-opus-4-8"]
        _write_shard(tmp_path, "clean_room-1-16", _metered_rows("claude-opus-4-8"), models)
        _write_shard(tmp_path, "under_load-4-5", _unmetered_rows("claude-opus-4-8"), models)

        with pytest.raises(UnmeteredShardError) as excinfo:
            verify_publishable(tmp_path)

        assert "under_load-4-5" in str(excinfo.value)
        assert "clean_room-1-16" not in str(excinfo.value)

    def test_empty_directory_is_publishable(self, tmp_path: Path) -> None:
        verify_publishable(tmp_path)


class TestShardReport:
    def test_names_every_zero_cost_model_once(self, tmp_path: Path) -> None:
        models = ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"]
        rows = [
            *_metered_rows("claude-opus-4-8"),
            *_unmetered_rows("claude-sonnet-5"),
            *_unmetered_rows("claude-haiku-4-5"),
        ]
        _write_shard(tmp_path, "under_load-3-5", rows, models)

        shards = contaminated_shards(tmp_path)

        assert [shard.shard for shard in shards] == ["under_load-3-5"]
        assert shards[0].zero_cost_models == ("claude-sonnet-5", "claude-haiku-4-5")

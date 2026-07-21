"""``t3 eval verify-benchmark-publish`` gates the weekly dashboard commit.

Driven through the real typer CLI over real ``render_matrix_html`` artifacts, so
the exit code the publish job branches on is exercised end to end.
"""

from pathlib import Path

from typer.testing import CliRunner

from teatree.cli import app
from teatree.eval.matrix import MatrixRow, render_matrix_html
from teatree.eval.models import EvalSpec

runner = CliRunner()


def _write_shard(directory: Path, name: str, *, cost_usd: float) -> None:
    spec = EvalSpec(
        name="alpha",
        scenario="scenario alpha",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        judge=None,
    )
    rows = [
        MatrixRow(
            scenario="alpha",
            model="claude-sonnet-5",
            passed=False,
            score=0.0,
            trials=1,
            skipped=False,
            cost_usd=cost_usd,
        )
    ]
    (directory / f"eval-benchmark-{name}.html").write_text(
        render_matrix_html(rows, ["claude-sonnet-5"], [spec]), encoding="utf-8"
    )


class TestVerifyBenchmarkPublish:
    def test_metered_shards_exit_zero(self, tmp_path: Path) -> None:
        _write_shard(tmp_path, "clean_room-1-16", cost_usd=0.31)

        result = runner.invoke(app, ["eval", "verify-benchmark-publish", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "publishable" in result.output

    def test_unmetered_shard_exits_one_and_names_the_shard(self, tmp_path: Path) -> None:
        _write_shard(tmp_path, "clean_room-1-16", cost_usd=0.31)
        _write_shard(tmp_path, "under_load-3-5", cost_usd=0.0)

        result = runner.invoke(app, ["eval", "verify-benchmark-publish", str(tmp_path)])

        assert result.exit_code == 1
        assert "under_load-3-5" in result.output
        assert "claude-sonnet-5" in result.output

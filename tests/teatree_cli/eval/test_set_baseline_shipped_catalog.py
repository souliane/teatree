"""Regression for duplicate model ids in the shipped abstract tiers."""

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from teatree.agents.model_tiering import TIER_MODELS
from teatree.cli import app
from teatree.eval.models import EvalSpec


def test_shipped_sonnet_pass_is_labeled_balanced(tmp_path: Path) -> None:
    assert TIER_MODELS["cheap"] == TIER_MODELS["balanced"]
    matrix = tmp_path / "matrix.json"
    matrix.write_text(
        json.dumps(
            {
                "models": [TIER_MODELS["cheap"]],
                "scenarios": [
                    {
                        "name": "alpha",
                        "results": {
                            TIER_MODELS["cheap"]: {
                                "passed": True,
                                "skipped": False,
                                "errored": False,
                                "score": 1.0,
                                "trials": 1,
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    spec = EvalSpec(
        name="alpha",
        scenario="sc",
        agent_path="skills/code/SKILL.md",
        prompt="p",
        matchers=(),
        source_path=Path("x.yaml"),
    )
    out = tmp_path / "baseline.yaml"
    with patch("teatree.cli.eval.set_baseline.discover_specs", return_value=[spec]):
        result = CliRunner().invoke(app, ["eval", "set-baseline", "--from", str(matrix), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(out.read_text(encoding="utf-8"))["scenarios"] == {"alpha": "balanced"}

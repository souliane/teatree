from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from teatree.cli.eval.all import run_ai_lane
from teatree.eval.models import EvalSpec
from teatree.llm.anthropic_limits import CreditExhaustedError


def test_credit_exhaustion_parks_the_full_ai_lane(tmp_path: Path) -> None:
    spec = EvalSpec(
        name="credit-case",
        scenario="measure credit case",
        agent_path="skills/code/SKILL.md",
        prompt="run",
        matchers=(),
        source_path=tmp_path / "spec.yaml",
    )
    with (
        patch("teatree.cli.eval.all.make_runner", return_value=object()),
        patch("teatree.cli.eval.all.run_specs", side_effect=CreditExhaustedError("credits exhausted")),
        pytest.raises(typer.Exit) as exit_info,
    ):
        run_ai_lane([spec], backend="api", target_dir=tmp_path)

    assert exit_info.value.exit_code == 75

from pathlib import Path

import pytest

from teatree.eval.models import CachedPass, EvalRun, EvalSpec
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.report import ScenarioResult
from teatree.eval.summary_model import summary_model


def test_summary_model_prefers_fresh_result(tmp_path: Path) -> None:
    spec = EvalSpec(name="one", scenario="case", agent_path="agent.md", prompt="act", matchers=(), source_path=tmp_path)
    run = EvalRun(
        spec_name="one",
        tool_calls=(),
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.0,
    )
    result = ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=False)
    cached = CachedPass("two", "clean_room", "a" * 64, "run", "now", "head", "other")
    assert summary_model([result], [cached]) == spec.model


def test_summary_model_uses_catalog_or_cached_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec = EvalSpec(name="one", scenario="case", agent_path="agent.md", prompt="act", matchers=(), source_path=tmp_path)
    result = PassAtKResult(
        spec_name="one", trials=1, passes=1, require="any", skipped=False, cost_usd=0.0, trial_results=()
    )
    cached = CachedPass("two", "clean_room", "a" * 64, "run", "now", "head", "other")
    monkeypatch.setattr("teatree.eval.summary_model.find_spec", lambda _name: spec)
    assert summary_model([result], [cached]) == spec.model
    monkeypatch.setattr("teatree.eval.summary_model.find_spec", lambda _name: None)
    assert summary_model([result], [cached]) == "other"
    assert summary_model([result]) == "unknown"

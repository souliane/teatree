"""``t3 eval run --preset`` — apply a model-tier PRESET at the per-scenario seam.

End-to-end through the real typer CLI with a stubbed api runner — no live model
call, no concrete model-id literal (asserted via ``TIER_MODELS``/``resolve_tier``).
"""

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.agents.model_tiering import resolve_tier
from teatree.cli import app
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.llm.credentials import AnthropicSubscriptionCredential

if TYPE_CHECKING:
    from collections.abc import Iterator

_PASSING_CALL = (EvalToolCall(name="Bash", input={"command": "git worktree add ../wt HEAD"}, turn=1),)


@pytest.fixture(autouse=True)
def _hermetic_credential() -> "Iterator[None]":
    with (
        patch("teatree.credential_config.resolve_eval_credential", lambda **_: AnthropicSubscriptionCredential()),
        patch.object(AnthropicSubscriptionCredential, "export", return_value="oauth-test"),
        patch.dict("os.environ", {"T3_EVAL_IN_CONTAINER": "1"}),
    ):
        yield


def _spec(name: str, *, tier: str = "", model: str = "") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario for {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=Path("/tmp/spec.yaml"),
        tier=tier,
        model=model,
    )


def _run(spec: EvalSpec) -> EvalRun:
    return EvalRun(
        spec_name=spec.name,
        tool_calls=_PASSING_CALL,
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.05,
        billed_model=spec.model or None,
    )


class _StubRunner:
    """Records every (resolved) model it was asked to run, for assertions."""

    seen_models: ClassVar[list[str]] = []

    def __init__(self, *_: object, **__: object) -> None: ...

    def run(self, spec: EvalSpec) -> EvalRun:
        _StubRunner.seen_models.append(spec.model)
        return _run(spec)


@pytest.fixture(autouse=True)
def _reset_seen() -> None:
    _StubRunner.seen_models = []


def _invoke(args: list[str], specs: list[EvalSpec]) -> object:
    with (
        patch("teatree.cli.eval.app.discover_specs", return_value=specs),
        patch("teatree.eval.backends.ApiInProcessRunner", _StubRunner),
    ):
        return CliRunner().invoke(app, ["eval", "run", *args])


class TestPresetForcesTheApiBackend:
    def test_cheap_preset_forces_every_scenario_onto_the_cheap_tier(self) -> None:
        specs = [_spec("alpha", tier="frontier"), _spec("beta", tier="balanced")]
        out = _invoke(["--preset", "cheap", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        assert set(_StubRunner.seen_models) == {resolve_tier("cheap")}

    def test_frontier_preset_forces_every_scenario_onto_the_frontier_tier(self) -> None:
        specs = [_spec("alpha", tier="cheap")]
        out = _invoke(["--preset", "frontier", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        assert set(_StubRunner.seen_models) == {resolve_tier("frontier")}


class TestPresetPrecedence:
    def test_explicit_scenario_model_wins_over_the_preset(self) -> None:
        specs = [_spec("alpha", model="pinned-model-x")]
        out = _invoke(["--preset", "cheap", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        assert _StubRunner.seen_models == ["pinned-model-x"]


class TestBaselinePreset:
    def test_absent_scenario_falls_through_to_its_own_yaml_tier(self, tmp_path: Path) -> None:
        baseline = tmp_path / "baseline.yaml"
        baseline.write_text("scenarios:\n  other: cheap\n", encoding="utf-8")
        specs = [_spec("alpha", tier="frontier")]
        with patch("teatree.eval.presets.BASELINE_PRESET_PATH", baseline):
            out = _invoke(["--preset", "baseline", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        assert _StubRunner.seen_models == [resolve_tier("frontier")]

    def test_mapped_scenario_uses_its_baseline_tier(self, tmp_path: Path) -> None:
        baseline = tmp_path / "baseline.yaml"
        baseline.write_text("scenarios:\n  alpha: cheap\n", encoding="utf-8")
        specs = [_spec("alpha", tier="frontier")]
        with patch("teatree.eval.presets.BASELINE_PRESET_PATH", baseline):
            out = _invoke(["--preset", "baseline", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        assert _StubRunner.seen_models == [resolve_tier("cheap")]


class TestPresetMutualExclusivity:
    def test_preset_and_model_are_mutually_exclusive(self) -> None:
        specs = [_spec("alpha")]
        out = _invoke(["--preset", "cheap", "--model", "x", "--no-persist"], specs)
        assert out.exit_code != 0
        assert "mutually exclusive" in out.output

    def test_preset_and_benchmark_are_mutually_exclusive(self) -> None:
        specs = [_spec("alpha")]
        out = _invoke(["--preset", "cheap", "--benchmark", "--no-persist"], specs)
        assert out.exit_code != 0
        assert "mutually exclusive" in out.output

    def test_preset_and_models_are_mutually_exclusive(self) -> None:
        specs = [_spec("alpha")]
        out = _invoke(["--preset", "cheap", "--models", "opus,sonnet", "--no-persist"], specs)
        assert out.exit_code != 0
        assert "mutually exclusive" in out.output

    def test_unknown_preset_name_exits_2(self) -> None:
        specs = [_spec("alpha")]
        out = _invoke(["--preset", "does-not-exist", "--no-persist"], specs)
        assert out.exit_code == 2
        assert "unknown preset" in out.output

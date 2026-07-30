"""``t3 eval benchmark --presets`` — compare PRESET columns instead of raw model@effort variants.

End-to-end through the real typer CLI with a stubbed api runner. ``summarize_benchmark``
works unmodified: each preset resolves to its own column TAG (``cheap``/``baseline``/
``default``), never the per-scenario resolved model, so grouping by variant name
matches the columns exactly even when two scenarios under one column run
different concrete models.
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


def _spec(name: str, *, tier: str = "") -> EvalSpec:
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
    )


class _StubRunner:
    """Records every (spec name, resolved model) pair it was asked to run."""

    seen: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, *_: object, **__: object) -> None: ...

    def run(self, spec: EvalSpec) -> EvalRun:
        _StubRunner.seen.append((spec.name, spec.model))
        return _run(spec)


@pytest.fixture(autouse=True)
def _reset_seen() -> None:
    _StubRunner.seen = []


def _invoke(args: list[str], specs: list[EvalSpec]) -> object:
    with (
        patch("teatree.cli.eval.benchmark.discover_specs", return_value=specs),
        patch("teatree.eval.backends.ApiInProcessRunner", _StubRunner),
        patch("teatree.eval.persistence.current_git_sha", return_value=""),
    ):
        return CliRunner().invoke(app, ["eval", "benchmark", *args], env={"T3_EVAL_IN_CONTAINER": "1"})


class TestPresetColumns:
    def test_each_scenario_resolves_its_own_model_under_a_preset_column(self) -> None:
        # cheap/frontier scenarios differ by their OWN tier under the "default" column,
        # yet both are grouped under the SAME "default" tag.
        specs = [_spec("alpha", tier="cheap"), _spec("beta", tier="frontier")]
        out = _invoke(["--presets", "default", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        assert dict(_StubRunner.seen) == {"alpha": resolve_tier("cheap"), "beta": resolve_tier("frontier")}
        assert "default" in out.output

    def test_cheap_preset_forces_every_scenario_onto_the_cheap_tier(self) -> None:
        specs = [_spec("alpha", tier="frontier"), _spec("beta", tier="balanced")]
        out = _invoke(["--presets", "cheap", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        assert set(_StubRunner.seen) == {("alpha", resolve_tier("cheap")), ("beta", resolve_tier("cheap"))}

    def test_multiple_preset_columns_produce_non_colliding_rows(self) -> None:
        specs = [_spec("alpha")]
        out = _invoke(["--presets", "cheap,frontier,default", "--format", "json", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        assert _StubRunner.seen == [
            ("alpha", resolve_tier("cheap")),
            ("alpha", resolve_tier("frontier")),
            ("alpha", resolve_tier("balanced")),  # default: no tier declared -> DEFAULT_TIER
        ]
        assert '"variant": "cheap"' in out.output
        assert '"variant": "frontier"' in out.output
        assert '"variant": "default"' in out.output

    def test_baseline_preset_column_falls_through_for_an_unmapped_scenario(self, tmp_path: Path) -> None:
        baseline = tmp_path / "baseline.yaml"
        baseline.write_text("scenarios: {}\n", encoding="utf-8")
        specs = [_spec("alpha", tier="frontier")]
        with patch("teatree.eval.presets.BASELINE_PRESET_PATH", baseline):
            out = _invoke(["--presets", "baseline", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        assert _StubRunner.seen == [("alpha", resolve_tier("frontier"))]


class TestModelsAndPresetsMutuallyExclusive:
    def test_neither_flag_is_rejected(self) -> None:
        out = _invoke(["--no-persist"], [_spec("alpha")])
        assert out.exit_code == 2
        assert "exactly one" in out.output

    def test_both_flags_is_rejected(self) -> None:
        out = _invoke(["--models", "opus", "--presets", "cheap", "--no-persist"], [_spec("alpha")])
        assert out.exit_code == 2
        assert "exactly one" in out.output

    def test_unknown_preset_name_exits_2(self) -> None:
        out = _invoke(["--presets", "does-not-exist", "--no-persist"], [_spec("alpha")])
        assert out.exit_code == 2
        assert "unknown preset" in out.output

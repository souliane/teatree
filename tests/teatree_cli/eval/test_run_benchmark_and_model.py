"""``t3 eval run`` --benchmark / --model / default lane, end-to-end through typer.

``--benchmark`` runs every scenario against ALL three tier models (resolved
through the single TIER_MODELS constant) and renders the matrix + an HTML
dashboard. ``--model`` forces the whole suite onto one model. The DEFAULT run is
per-scenario tier/phase, single-trial, with NO pass@k and NO escalation. The
``--only`` flag is gone; positional + ``--lane`` are the surviving filters.

These exercise the real CLI with a stubbed api runner — no live model call. No
concrete model-id string literal: the benchmark column models are asserted via
TIER_MODELS / resolve_tier.
"""

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.agents.model_tiering import TIER_MODELS, resolve_tier
from teatree.cli import app
from teatree.cli.eval.app_helpers import BENCHMARK_TIERS
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.llm.credentials import AnthropicApiKeyCredential

if TYPE_CHECKING:
    from collections.abc import Iterator

_PASSING_CALL = (EvalToolCall(name="Bash", input={"command": "git worktree add ../wt HEAD"}, turn=1),)


@pytest.fixture(autouse=True)
def _hermetic_api() -> "Iterator[None]":
    # Bypass the config-aware credential factory (which reads the DB) to the default
    # credential so these CLI lanes run DB-free; per-account routing has its own tests.
    with (
        patch("teatree.credential_config.resolve_api_key_credential", lambda **_: AnthropicApiKeyCredential()),
        patch.object(AnthropicApiKeyCredential, "export", return_value="sk-test"),
        patch.dict("os.environ", {"T3_EVAL_IN_CONTAINER": "1"}),
    ):
        yield


def _spec(name: str, *, lane: str = "clean_room") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario for {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=Path("/tmp/spec.yaml"),
        lane=lane,
    )


def _run(spec: EvalSpec) -> EvalRun:
    return EvalRun(
        spec_name=spec.name,
        tool_calls=_PASSING_CALL,
        text_blocks=("SECRET_TRANSCRIPT_LEAK_xyz",),
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


class TestBenchmark:
    def test_runs_every_tier_model(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        out = _invoke(["--benchmark", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        # The benchmark expands to the concrete model behind each of the 3 tiers.
        expected = {resolve_tier(t) for t in BENCHMARK_TIERS}
        assert set(_StubRunner.seen_models) == expected
        # Sanity: exactly the three TIER_MODELS values, no concrete literal here.
        assert expected == set(TIER_MODELS.values())

    def test_writes_html_dashboard(self, tmp_path: Path) -> None:
        specs = [_spec("alpha")]
        html_out = tmp_path / "matrix.html"
        out = _invoke(["--benchmark", "--no-persist", "--transcript-html", str(html_out)], specs)
        assert out.exit_code == 0, out.output
        body = html_out.read_text(encoding="utf-8")
        assert "<table" in body
        assert "alpha" in body
        # Each tier model is a column header.
        for tier in BENCHMARK_TIERS:
            assert resolve_tier(tier) in body


class TestModelForce:
    def test_forces_whole_suite_onto_one_model(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        out = _invoke(["--model", "candidate-model-x", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        # Every spec ran on the forced model — nothing else.
        assert set(_StubRunner.seen_models) == {"candidate-model-x"}

    def test_model_and_benchmark_mutually_exclusive(self) -> None:
        specs = [_spec("alpha")]
        out = _invoke(["--model", "x", "--benchmark", "--no-persist"], specs)
        assert out.exit_code != 0
        assert "mutually exclusive" in out.output


class TestDefaultRunLane:
    def test_default_run_uses_per_scenario_tier_no_passk_no_escalate(self) -> None:
        # The default run resolves each spec's tier/phase to a concrete model (one
        # trial each) — NOT pass@k, NOT a forced model. Two specs → two runs.
        specs = [_spec("alpha", lane="clean_room"), _spec("beta", lane="clean_room")]
        out = _invoke(["--backend", "api", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        # Single trial per spec (no pass@k k>1 fan-out) and each ran on its
        # resolved DEFAULT_TIER model (the specs declare no tier → default tier).
        assert len(_StubRunner.seen_models) == 2
        assert set(_StubRunner.seen_models) == {resolve_tier("balanced")}

    def test_positional_name_filter_runs_one_scenario(self) -> None:
        specs = [_spec("alpha"), _spec("beta"), _spec("gamma")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.app_helpers.discover_specs", return_value=specs),
            patch("teatree.eval.discovery.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ApiInProcessRunner", _StubRunner),
        ):
            out = CliRunner().invoke(app, ["eval", "run", "beta", "--backend", "api", "--no-persist"])
        assert out.exit_code == 0, out.output
        assert "beta" in out.output
        assert "alpha" not in out.output

    def test_lane_filter_scopes_the_catalog(self) -> None:
        specs = [_spec("alpha", lane="clean_room"), _spec("beta", lane="under_load")]
        out = _invoke(["--lane", "clean_room", "--backend", "api", "--no-persist"], specs)
        assert out.exit_code == 0, out.output
        assert "alpha" in out.output
        assert "PASS beta" not in out.output


class TestSummaryMd:
    def test_writes_sanitized_summary_without_transcript(self, tmp_path: Path) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        out_md = tmp_path / "summary.md"
        out = _invoke(["--backend", "api", "--no-persist", "--summary-md", str(out_md)], specs)
        assert out.exit_code == 0, out.output
        body = out_md.read_text(encoding="utf-8")
        assert "alpha" in body
        assert "beta" in body
        assert "SECRET_TRANSCRIPT_LEAK_xyz" not in body
        assert "| scenario | lane | verdict | trials |" in body

"""``t3 eval run --only`` + ``--summary-md`` end-to-end through the typer CLI.

The selective-PR workflow runs ``t3 eval run --only "<names>" --summary-md
"$GITHUB_STEP_SUMMARY"``: ``--only`` restricts the catalog to exactly the named
scenarios (an unknown name fails loud, never silently drops), and ``--summary-md``
writes the SANITIZED aggregate dashboard markdown (no transcript) to the path.
These exercise the single-trial path through the real CLI with a stubbed api
runner, so no live model call happens.
"""

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.llm.credentials import AnthropicApiKeyCredential

if TYPE_CHECKING:
    from collections.abc import Iterator

_PASSING_CALL = (EvalToolCall(name="Bash", input={"command": "git worktree add ../wt HEAD"}, turn=1),)


@pytest.fixture(autouse=True)
def _hermetic_api() -> "Iterator[None]":
    with (
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


def _run(spec_name: str) -> EvalRun:
    return EvalRun(
        spec_name=spec_name,
        tool_calls=_PASSING_CALL,
        text_blocks=("SECRET_TRANSCRIPT_LEAK_xyz",),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.05,
    )


class _StubRunner:
    def __init__(self, *_: object, **__: object) -> None: ...

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec.name)


class TestOnlyFlag:
    def test_only_runs_exactly_the_named_subset(self) -> None:
        specs = [_spec("alpha"), _spec("beta"), _spec("gamma")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.only_filter.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ApiInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(
                app, ["eval", "run", "--backend", "api", "--no-persist", "--only", "alpha,gamma"]
            )
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert "gamma" in result.output
        assert "PASS beta" not in result.output

    def test_unknown_only_name_exits_non_zero(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.only_filter.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ApiInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(
                app, ["eval", "run", "--backend", "api", "--no-persist", "--only", "alpha,ghost"]
            )
        assert result.exit_code != 0
        assert "ghost" in result.output


class TestSummaryMd:
    def test_writes_sanitized_summary_without_transcript(self, tmp_path: Path) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        out = tmp_path / "summary.md"
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ApiInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(
                app, ["eval", "run", "--backend", "api", "--no-persist", "--summary-md", str(out)]
            )
        assert result.exit_code == 0, result.output
        body = out.read_text(encoding="utf-8")
        assert "alpha" in body
        assert "beta" in body
        assert "SECRET_TRANSCRIPT_LEAK_xyz" not in body
        assert "| scenario | lane | verdict | trials |" in body

    def test_no_file_written_when_summary_md_absent(self, tmp_path: Path) -> None:
        specs = [_spec("alpha")]
        out = tmp_path / "summary.md"
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ApiInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "api", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert not out.exists()

"""Metered-runner construction chokepoint fitness function (souliane/teatree#2328).

The metered ``ApiInProcessRunner`` must be built ONLY through
``teatree.eval.backends.make_runner`` — the single non-Docker path that resolves
the metered ``ANTHROPIC_API_KEY`` (via ``AnthropicApiKeyCredential().export()``)
before a metered runner exists. A lane that constructs ``ApiInProcessRunner(...)``
directly bypasses that resolver, so on a host ``--local`` run (key only in
``pass``, not the env) the isolated ``claude`` child authenticates as nothing and
the run reports a zero-cost auth failure. That is exactly the bypass the
``t3 eval benchmark`` and ``t3 eval run --trials k`` lanes had.

This AST gate walks the eval source tree and turns RED if any module OTHER than
the allowed chokepoint constructs ``ApiInProcessRunner`` by name — so the bypass
class cannot regress. The construction is allowed only in
``teatree.eval.backends`` (the ``make_runner`` factory). Modeled on
``tests/quality/test_spawn_model_chokepoint.py``.
"""

# test-path: cross-cutting
import ast
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.eval.backends import API_BACKEND, TRANSCRIPT_BACKEND, make_runner
from teatree.eval.judge import ClaudeJudge
from teatree.eval.models import EvalRun, EvalSpec, JudgeSpec, Matcher
from teatree.llm.credentials import AnthropicApiKeyCredential, AnthropicSubscriptionCredential, CredentialError

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "teatree"

#: The eval source subtrees scanned. Every metered-runner construction lives
#: under one of these, so a bypass anywhere in eval is caught.
_SCANNED_ROOTS = (
    _SRC_ROOT / "cli" / "eval",
    _SRC_ROOT / "eval",
)

_RUNNER_SYMBOL = "ApiInProcessRunner"

#: The ONLY module allowed to construct the metered runner — the ``make_runner``
#: factory that resolves the API key first.
_ALLOWED_MODULES = frozenset({"teatree.eval.backends"})


def _module_dotted(path: Path) -> str:
    rel = path.resolve().relative_to(_SRC_ROOT.parent).with_suffix("")
    parts = rel.parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _constructs_runner(path: Path) -> list[int]:
    """Lines in *path* that call ``ApiInProcessRunner(...)`` as a bare constructor."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == _RUNNER_SYMBOL:
            hits.add(node.lineno)
    return sorted(hits)


def _eval_modules() -> list[Path]:
    seen: dict[Path, None] = {}
    for root in _SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            seen[path.resolve()] = None
    return list(seen)


class TestMeteredRunnerChokepoint:
    def test_scanned_roots_exist(self) -> None:
        for root in _SCANNED_ROOTS:
            assert root.is_dir(), root

    def test_allowed_module_actually_constructs_the_runner(self) -> None:
        # The chokepoint is real: backends.make_runner genuinely builds the runner.
        # If this stops being true the allow-list is stale, not the gate.
        backends = _SRC_ROOT / "eval" / "backends.py"
        assert _constructs_runner(backends), "teatree.eval.backends no longer constructs ApiInProcessRunner"

    def test_no_eval_module_constructs_the_runner_outside_the_chokepoint(self) -> None:
        offenders: dict[str, list[int]] = {}
        for path in _eval_modules():
            module = _module_dotted(path)
            if module in _ALLOWED_MODULES:
                continue
            lines = _constructs_runner(path)
            if lines:
                offenders[module] = lines
        assert not offenders, (
            f"{_RUNNER_SYMBOL} is constructed directly outside teatree.eval.backends — "
            "the API-key resolution in make_runner is bypassed, so a host --local "
            f"metered run authenticates as nothing (souliane/teatree#2328): {offenders}"
        )

    def test_predicate_catches_a_bare_construction(self, tmp_path: Path) -> None:
        bait = tmp_path / "bait.py"
        bait.write_text(
            "from teatree.eval.api_runner import ApiInProcessRunner, ApiRunnerParams\n"
            "runner = ApiInProcessRunner(ApiRunnerParams(require_executed=True))\n",
            encoding="utf-8",
        )
        assert _constructs_runner(bait)

    def test_predicate_ignores_make_runner_routing(self, tmp_path: Path) -> None:
        clean = tmp_path / "clean.py"
        clean.write_text(
            "from teatree.eval.backends import API_BACKEND, ApiRunnerParams, make_runner\n"
            "runner = make_runner(API_BACKEND, ApiRunnerParams(require_executed=True))\n",
            encoding="utf-8",
        )
        assert not _constructs_runner(clean)


def _judge_spec() -> EvalSpec:
    return EvalSpec(
        name="explains_faithfully",
        scenario="agent explains the change faithfully",
        agent_path="skills/code/SKILL.md",
        prompt="explain",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        source_path=Path("/tmp/spec.yaml"),
        judge=JudgeSpec(rubric="x"),
    )


def _graded_run() -> EvalRun:
    return EvalRun(
        spec_name="explains_faithfully",
        tool_calls=(),
        text_blocks=("explanation",),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


class TestEveryMeteredEntrypointFailsLoudWithoutAKey(TestCase):
    """Behavioral anti-vacuity (#2707 finding 4): each eval entrypoint fails loud with no credential.

    The AST-shape gate above proves the runner is built only through the
    chokepoint, but it would still pass if ``export()`` were removed from the
    chokepoint — the enforcement could silently regress. These tests close that
    hole: with NEITHER credential in the env and an empty ``pass`` store, EACH
    fresh-run entrypoint — (a) the api runner factory, (b) ``t3 eval benchmark``
    Docker pre-check, (c) the judge — raises :class:`CredentialError` BEFORE doing
    any work. The default eval credential is the subscription OAuth token (reversing
    #2707), so BOTH vars are cleared. Removing the ``export`` from any one
    entrypoint turns its test RED.
    """

    @staticmethod
    def _no_key() -> None:
        os.environ.pop(AnthropicApiKeyCredential.spec.env_var, None)
        os.environ.pop(AnthropicSubscriptionCredential.spec.env_var, None)

    def test_api_runner_factory_fails_loud(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.llm.credentials.read_pass", return_value=""),
        ):
            self._no_key()
            with pytest.raises(CredentialError):
                make_runner(API_BACKEND)

    def test_transcript_factory_never_requires_a_key(self) -> None:
        # The replay lane runs no model; it must build keyless (the negative control).
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.llm.credentials.read_pass", return_value=""),
        ):
            self._no_key()
            assert make_runner(TRANSCRIPT_BACKEND) is not None

    def test_benchmark_docker_precheck_fails_loud_before_any_docker_work(self) -> None:
        from teatree.cli.eval.docker import run_eval_in_docker  # noqa: PLC0415

        module = "teatree.cli.eval.docker"
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.llm.credentials.read_pass", return_value=""),
            patch(f"{module}.shutil.which", return_value="/usr/bin/docker"),
            patch(f"{module}._image_present", return_value=True) as image_present,
            patch(f"{module}.run_streamed", return_value=0) as streamed,
        ):
            self._no_key()
            with pytest.raises(CredentialError):
                run_eval_in_docker(["benchmark", "--models", "claude-opus-4-8@xhigh"])
        image_present.assert_not_called()
        streamed.assert_not_called()

    def test_metered_judge_fails_loud(self) -> None:
        # claude present + a real run to grade → the billed judge call must fail loud.
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.llm.credentials.read_pass", return_value=""),
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
        ):
            self._no_key()
            with pytest.raises(CredentialError):
                ClaudeJudge().grade(_judge_spec(), _graded_run())

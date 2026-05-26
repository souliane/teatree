"""Run every shipped eval scenario against its fail/pass fixtures.

A scenario is **anti-vacuous** when:

*   against its ``<name>_fail.stream.jsonl`` fixture the scenario verdict
    is FAIL (so a regressing agent would surface red), and
*   against its ``<name>_pass.stream.jsonl`` fixture (when present) the
    scenario verdict is PASS (so a compliant agent stays green).

A scenario with only a ``_fail`` fixture is still validated for the FAIL
direction. A scenario with neither is skipped — discovery still asserts
its YAML loads cleanly, but the matcher-behaviour assertion needs at
least the fail fixture.

This is the canonical "would this scenario catch a regression?" test.
A YAML that ships without an anti-vacuous fail fixture is silently
toothless, so this test runs on every PR.
"""

import dataclasses
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from teatree.eval.discovery import discover_specs
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate
from teatree.eval.runner import ClaudePRunner

FIXTURES = Path(__file__).parent / "fixtures"


@dataclasses.dataclass
class _FakeCompleted:
    stdout: str
    stderr: str = ""
    returncode: int = 0


def _run_against_fixture(spec: EvalSpec, fixture_text: str, tmp_path: Path) -> bool:
    """Return ``True`` when the scenario passed against ``fixture_text``."""

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(stdout=fixture_text)

    with (
        patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
        patch("teatree.utils.run.subprocess.run", side_effect=_fake_run),
    ):
        run = ClaudePRunner(workspace=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _specs_with_fixtures() -> list[tuple[EvalSpec, Path | None, Path | None]]:
    rows: list[tuple[EvalSpec, Path | None, Path | None]] = []
    for spec in discover_specs():
        fail = FIXTURES / f"{spec.name}_fail.stream.jsonl"
        pass_ = FIXTURES / f"{spec.name}_pass.stream.jsonl"
        rows.append((spec, fail if fail.is_file() else None, pass_ if pass_.is_file() else None))
    return rows


def _specs_with_noop_fixtures() -> list[tuple[EvalSpec, Path]]:
    """Scenarios that ship a ``_noop`` fixture proving non-vacuity.

    A ``_noop`` fixture captures an agent transcript with no tool calls
    at all — a positive matcher that is genuinely required (vs. an
    only-negative vacuous matcher) must report RED against this fixture.
    """
    rows: list[tuple[EvalSpec, Path]] = []
    for spec in discover_specs():
        noop = FIXTURES / f"{spec.name}_noop.stream.jsonl"
        if noop.is_file():
            rows.append((spec, noop))
    return rows


@pytest.mark.parametrize(
    ("spec", "fail_fixture", "pass_fixture"),
    _specs_with_fixtures(),
    ids=lambda v: v.name if isinstance(v, EvalSpec) else (v.name if isinstance(v, Path) else "none"),
)
class TestScenarioFixtures:
    def test_fail_fixture_drives_scenario_red(
        self,
        spec: EvalSpec,
        fail_fixture: Path | None,
        pass_fixture: Path | None,
        tmp_path: Path,
    ) -> None:
        _ = pass_fixture
        if fail_fixture is None:
            pytest.skip(f"no fail fixture for {spec.name}")
        passed = _run_against_fixture(spec, fail_fixture.read_text(encoding="utf-8"), tmp_path)
        assert passed is False, (
            f"scenario {spec.name!r} stayed GREEN against {fail_fixture.name} — "
            "the matchers are toothless. Either tighten the matcher or strengthen the fixture."
        )

    def test_pass_fixture_drives_scenario_green(
        self,
        spec: EvalSpec,
        fail_fixture: Path | None,
        pass_fixture: Path | None,
        tmp_path: Path,
    ) -> None:
        _ = fail_fixture
        if pass_fixture is None:
            pytest.skip(f"no pass fixture for {spec.name}")
        passed = _run_against_fixture(spec, pass_fixture.read_text(encoding="utf-8"), tmp_path)
        assert passed is True, (
            f"scenario {spec.name!r} went RED against {pass_fixture.name} — "
            "either the fixture violates the rule or the matchers over-fit."
        )


@pytest.mark.parametrize(
    ("spec", "noop_fixture"),
    _specs_with_noop_fixtures(),
    ids=lambda v: v.name if isinstance(v, EvalSpec) else (v.name if isinstance(v, Path) else "none"),
)
def test_noop_transcript_drives_scenario_red(spec: EvalSpec, noop_fixture: Path, tmp_path: Path) -> None:
    """A scenario must FAIL against an empty-tool-call transcript.

    Scenarios composed only of negative matchers (``no_tool_call_matching``)
    are vacuously satisfied by a no-op agent transcript. Adding a positive
    matcher closes that hole. This test asserts the positive matcher is
    actually wired up — if it is omitted, the no-op transcript goes
    silently green and the scenario is toothless.
    """
    passed = _run_against_fixture(spec, noop_fixture.read_text(encoding="utf-8"), tmp_path)
    assert passed is False, (
        f"scenario {spec.name!r} stayed GREEN against {noop_fixture.name} (no tool calls) — "
        "the scenario is satisfied by a no-op agent and therefore vacuous. "
        "Add a positive matcher that requires the expected tool call."
    )

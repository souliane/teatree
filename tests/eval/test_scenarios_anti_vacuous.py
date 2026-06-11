"""Run every shipped eval scenario against its fail/pass fixtures.

A scenario is **anti-vacuous** when:

*   against its ``<name>_fail.stream.jsonl`` fixture the scenario verdict
    is FAIL (so a regressing agent would surface red), and
*   against its ``<name>_pass.stream.jsonl`` fixture (when present) the
    scenario verdict is PASS (so a compliant agent stays green).

A scenario with only a ``_fail`` fixture is still validated for the FAIL
direction.

A **behavioral scenario with no fixtures at all FAILS LOUD** — it is never
skipped. A behavioral scenario is graded by matchers (``spec.matchers``);
a matcher set that is never exercised against a ``_fail`` fixture guards
nothing, yet a skipped scenario reports as passed. That is the "skip
counted as pass" / fake-green class (#2162): a scenario can ship with
zero fixtures and the suite reads green while the matcher is toothless.
The gate below (:func:`test_every_behavioral_scenario_ships_a_fail_fixture`)
makes that state a hard RED instead of a silent skip.

This is the canonical "would this scenario catch a regression?" test.
A YAML that ships without an anti-vacuous fail fixture is silently
toothless, so this test runs on every PR.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from teatree.eval.backends import SubscriptionTranscriptRunner
from teatree.eval.discovery import discover_specs
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import evaluate
from teatree.eval.sdk_runner import load_agent_definition

FIXTURES = Path(__file__).parent / "fixtures"


def _is_behavioral(spec: EvalSpec) -> bool:
    """A behavioral scenario is graded by matchers (vs. judge-only).

    A matcher-graded scenario needs a ``_fail`` fixture to prove the
    matchers actually catch the violating behaviour. A judge-only scenario
    (no matchers, a ``judge`` block) is graded by an LLM and is exempt from
    the fixture gate — there is no matcher to exercise against a transcript.
    """
    return bool(spec.matchers)


def _fixtureless_behavioral_specs() -> list[EvalSpec]:
    """Behavioral scenarios shipping NO ``_fail`` fixture — the gate's catch.

    A behavioral scenario whose ``_fail`` fixture is absent is never
    exercised against a violating transcript, so its matchers are
    unverified — the "skip counted as pass" class. This returns the
    offenders so the gate can fail loud naming each one.
    """
    return [
        spec
        for spec in discover_specs()
        if _is_behavioral(spec) and not (FIXTURES / f"{spec.name}_fail.stream.jsonl").is_file()
    ]


def _run_against_fixture(spec: EvalSpec, fixture_text: str, tmp_path: Path) -> bool:
    """Return ``True`` when the scenario passed against ``fixture_text``."""
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_text, encoding="utf-8")
    run = SubscriptionTranscriptRunner(transcript_dir=tmp_path).run(spec)
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


def test_every_behavioral_scenario_ships_a_fail_fixture() -> None:
    """The hardened gate: a behavioral scenario with no ``_fail`` fixture is RED.

    This is the #2162 enforcement. A behavioral (matcher-graded) scenario
    whose ``_fail`` fixture is absent is never exercised against a violating
    transcript — its matchers are unverified and could be toothless. The old
    behaviour SKIPPED such a scenario, and a skip reports as passed, so a
    fixtureless scenario read green while guarding nothing (the "skip counted
    as pass" / fake-green class).

    The fix is to FAIL LOUD here instead of skipping: a fixtureless behavioral
    scenario fails this assertion (exit non-zero), so it can never reach the
    suite green without a ``_fail`` fixture that proves the matchers catch the
    violation.
    """
    offenders = _fixtureless_behavioral_specs()
    assert not offenders, (
        "behavioral scenario(s) ship no `_fail` fixture, so their matchers are never "
        "exercised against a violating transcript — a fixtureless behavioral scenario "
        "would SKIP (counted as pass) and guard nothing. Backfill an anti-vacuous "
        "`<name>_fail.stream.jsonl` (drives the scenario RED) for each:\n"
        + "\n".join(f"  - {spec.name} ({spec.source_path.name})" for spec in offenders)
    )


def test_fixtureless_behavioral_scenario_is_caught_by_the_gate(tmp_path: Path) -> None:
    """Anti-vacuity proof for the hardened gate (#2162).

    Constructs a synthetic behavioral scenario that ships NO fixtures and
    asserts the gate predicate flags it. This proves the gate is not
    vacuous: a fixtureless behavioral scenario IS caught (the predicate
    returns it as an offender). If the gate's guard were reverted — e.g.
    by skipping fixtureless scenarios instead of catching them — this
    synthetic scenario would slip through and the proof would fail.

    The predicate keys on ``spec.matchers`` (behavioral) and the absence of
    a ``<name>_fail.stream.jsonl`` on disk, so a fabricated spec with a
    matcher and a name with no on-disk fixture must be flagged. A judge-only
    scenario (no matchers) is correctly NOT flagged — exercised here too so
    the gate does not over-fire on the exempt class.
    """
    _ = tmp_path
    fixtureless_behavioral = _fake_spec(
        name="__synthetic_fixtureless_behavioral__",
        matchers=(_TRIVIAL_MATCHER,),
    )
    judge_only = _fake_spec(name="__synthetic_judge_only__", matchers=())

    assert _is_behavioral(fixtureless_behavioral) is True
    assert _gate_flags(fixtureless_behavioral) is True, (
        "the hardened gate must FLAG a behavioral scenario with no `_fail` fixture; "
        "it did not — the guard is reverted and the fake-green class is back."
    )

    assert _is_behavioral(judge_only) is False
    assert _gate_flags(judge_only) is False, (
        "the gate must NOT flag a judge-only (matcherless) scenario — it is exempt "
        "from the fixture requirement (graded by an LLM, no matcher to exercise)."
    )


def test_every_declared_agent_section_resolves_against_its_skill() -> None:
    """A scenario's ``agent_sections`` must name real ``## `` sections of its skill.

    The token-cost lever sends only the named sections as the system prompt. A
    typo'd / renamed section would, at metered-run time, raise MissingSectionError
    — but only when the metered lane runs (weekly). This load-time guard turns a
    bad anchor into a RED on every PR: it resolves each declared section against
    the real on-disk SKILL.md the same way the runner does, so a drifted heading
    fails here, not silently months later.
    """
    from teatree.eval.context_budget import MissingSectionError, extract_sections  # noqa: PLC0415

    offenders: list[str] = []
    for spec in discover_specs():
        if not spec.agent_sections:
            continue
        text = load_agent_definition(spec.agent_path)
        try:
            extract_sections(text, spec.agent_sections)
        except MissingSectionError as exc:
            offenders.append(f"  - {spec.name} ({spec.source_path.name}): {exc}")
    assert not offenders, (
        "scenario(s) declare agent_sections that do not match any `## ` heading in "
        "their agent_path SKILL.md (a drifted/typo'd anchor would send an empty rule "
        "prompt and make the scenario vacuous at metered-run time):\n" + "\n".join(offenders)
    )


def _gate_flags(spec: EvalSpec) -> bool:
    """Whether the REAL gate predicate flags ``spec`` as an offender.

    This delegates to the production predicate
    :func:`_fixtureless_behavioral_specs` — the same function the gate
    (:func:`test_every_behavioral_scenario_ships_a_fail_fixture`) consumes —
    rather than re-implementing it. There is therefore ONE source of truth:
    the anti-vacuity proof exercises the real gate, so reverting the gate
    predicate (e.g. to ``return []``) turns the proof RED too.

    The synthetic spec is injected as the sole member of the discovered
    catalog (``discover_specs`` is the predicate's only input), so the proof
    stays independent of the live catalog's contents while still running the
    production code path.
    """
    with patch(f"{__name__}.discover_specs", return_value=[spec]):
        return spec in _fixtureless_behavioral_specs()


_TRIVIAL_MATCHER = Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value=".")


def _fake_spec(*, name: str, matchers: tuple[Any, ...]) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="synthetic",
        matchers=matchers,
        source_path=Path("synthetic.yaml"),
    )

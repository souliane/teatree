"""Anti-vacuity proof for the full-speed fan-out scenario ("full speed is understood").

The ``full_speed_fans_out_parallel_workers_not_serial`` scenario is the user's
headline eval. It pins that, under a full skill bundle and a long polluted context, a
``full``-speed directive over a backlog of independent tickets is moved by FANNING
OUT one worker sub-agent per ticket — not by working the backlog serially in the
main agent. The teeth checks:

*   the ``_fail`` fixture IS the serial drift (the main agent edits a ticket's
    source ``.py`` in the foreground and runs its tests), so the matchers must
    drive it RED;
*   REMOVING the matchers must turn that same ``_fail`` fixture GREEN — a matcher
    whose removal does not change the verdict guards nothing;
*   a DISCRIMINATING single-worker ``_fail`` fixture (dispatches ONE worker, then
    does the other two tickets serially in the main agent) must ALSO grade RED —
    so the scenario rejects a token single delegate, not only total serial work; and
*   the ``_pass`` fixture (three parallel worker dispatches, one per ticket)
    grades GREEN.

This is the deterministic PR-path proof; the live A/B pass@k measurement is the
gated/weekly metered step (the user's "powder-in-the-eyes" full-bundle A/B run),
not run here.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import SubscriptionTranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_SCENARIO = "full_speed_fans_out_parallel_workers_not_serial"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"
_SINGLE_WORKER_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_single_worker_fail.stream.jsonl"


def _grade(spec: EvalSpec, fixture: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = SubscriptionTranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"headline scenario {_SCENARIO!r} not discovered"
    return spec


def test_scenario_is_under_load_lane() -> None:
    assert _scenario_spec().lane == "under_load"


def test_scenario_carries_a_polluted_context_preamble() -> None:
    # The drift-inducing pollution must match the documented envelope's lower
    # bound — a realistic ~8k-token (~32k-char) polluted prior-session context,
    # not a token gesture (the same floor the flagship delegates_under_load proof
    # asserts). This guards against the preamble being trimmed below the size the
    # under_load lane's README envelope claims it ships at.
    preamble = _scenario_spec().context_preamble
    assert len(preamble) > 28000, (
        f"context_preamble is only {len(preamble)} chars — below the documented ~8k-token envelope"
    )


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the serial-drift _fail fixture (Edit + pytest in the main agent, no fan-out) must grade RED"
    )


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "the fan-out _pass fixture (one worker dispatched per ticket) must grade GREEN"
    )


def test_single_worker_then_serial_fixture_drives_scenario_red(tmp_path: Path) -> None:
    # The discriminator: a token single delegate plus serial work on the rest is
    # NOT full-speed fan-out. This fixture dispatches ONE worker (so the positive
    # any_of matcher is satisfied) but then edits the other two tickets' .py files
    # in the main agent — the no-serial-work negative matcher must still drive it
    # RED, so the scenario rejects "dispatch one, hand-do the rest", not only the
    # total-serial case.
    assert _grade(_scenario_spec(), _SINGLE_WORKER_FAIL_FIXTURE, tmp_path) is False, (
        "a single-worker-then-serial fixture (one Task dispatch, then Edit of the other tickets "
        "in the main agent) must grade RED — the negative no-serial-work matcher guards against a "
        "token delegate masking serial main-agent implementation"
    )


def test_removing_the_matchers_turns_the_fail_fixture_green(tmp_path: Path) -> None:
    # The teeth proof: a scenario with NO matchers cannot fail (nothing to assert),
    # so the same serial-drift _fail fixture goes GREEN. Because the real scenario
    # grades it RED (test above) and the matcherless variant grades it GREEN here,
    # the matchers are what catch the serial drift — they are not vacuous.
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "with the matchers removed the serial-drift fixture must go GREEN — if it stays RED, the "
        "fixture fails for a reason unrelated to the matchers and the proof is moot"
    )

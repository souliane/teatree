"""Promote a derived eval CANDIDATE into a live, graded scenario (#1933, #2346).

Phase-3b derives inert eval CANDIDATES (``eval_proposer``); this module is the
step that turns a candidate JSONL row into a REAL ``under_load`` scenario file
under ``evals/scenarios/`` plus its ``_pass``/``_fail`` replay fixtures under
``evals/fixtures/`` — the artifacts the deterministic replay test
(``tests/eval_replay/test_scenarios_anti_vacuous.py``) and the metered Agent-SDK
lane actually run.

Promotion is AUTO, gated by a NON-BYPASSABLE anti-vacuity guard
(:func:`guard_can_fail`). The user wants live evals, so a grounded candidate is
promoted without a human review queue — but ONLY when its grader is *proven* able
to FAIL. The guard is the dreaming-side enforcement of the standing rule "a drift
is not fixed until an anti-vacuous eval pins it": a candidate whose matchers do
NOT reject a known-bad transcript guards nothing, so it is REJECTED, never
written. The guard is structurally non-bypassable — :func:`promote_candidate`
calls it internally and returns a ``rejected`` outcome rather than writing any
file when it does not hold, so there is no code path that promotes an unproven
candidate.

What "proven able to fail" means, concretely and deterministically (no metered
model, no network):

*   A ``_fail`` transcript is synthesised from the candidate — an agent that
    *re-commits the cited drift* (the ``Edit``-in-main-agent shape the cited
    mistake describes). The candidate's matchers are run against it through the
    REAL grader (:func:`teatree.eval.report.evaluate`). The candidate is
    promotable ONLY when that verdict is FAIL (the matchers have teeth).
*   A ``_pass`` transcript — an agent that DELEGATES instead of editing — must
    grade PASS, so the scenario is not a tautology that fails everything.

Both checks run the same ``report.evaluate`` the suite uses, so a candidate that
clears the guard is graded identically once it lands. The metered AI lane that
runs the promoted scenario live is independent (and is being unblocked in
parallel by the eval-harness E2BIG fix); this guard is deterministic and does not
depend on it.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import yaml

from teatree.core.review.review_findings import find_bare_references, neutralize_bare_references
from teatree.eval.discovery import SCENARIOS_DIR
from teatree.eval.loader import load_eval_yaml
from teatree.eval.models import UNDER_LOAD_LANE, EvalSpec
from teatree.eval.report import evaluate
from teatree.hooks import banned_terms_scanner
from teatree.loops.dream.live_gate import (
    DEFAULT_LIVE_REQUIRE,
    DEFAULT_LIVE_TRIALS,
    LiveGate,
    LiveValidator,
    build_live_validator,
)
from teatree.loops.dream.promotion_outcome import PromotionOutcome
from teatree.loops.dream.transcript_synthesis import fail_transcript as _fail_transcript
from teatree.loops.dream.transcript_synthesis import pass_transcript as _pass_transcript
from teatree.loops.dream.transcript_synthesis import run_from_transcript as _run_from_transcript

__all__ = [
    "DEFAULT_LIVE_REQUIRE",
    "DEFAULT_LIVE_TRIALS",
    "LiveGate",
    "LiveValidator",
    "PromotionOutcome",
    "build_live_validator",
    "guard_can_fail",
    "loaded_scenario_names",
    "promote_candidate",
    "promote_proposals_file",
]

#: The skill whose rule a derived drift scenario pins. Drift candidates come from
#: the rules skill's instruction-following surface; the promoted scenario targets
#: it so the ``under_load`` bundle frames it correctly.
_DEFAULT_AGENT_PATH = "skills/rules/SKILL.md"

#: Fixtures live next to the scenarios under the same ``evals/`` root.
FIXTURES_DIR = SCENARIOS_DIR.parent / "fixtures"

_PROMOTED_SCENARIO_FILE = "promoted_drift.yaml"


#: The operator-derived free-text fields that flow from a candidate row into the
#: COMMITTED scenario YAML and its replay fixtures. ``drift_rule`` lands in the
#: scenario description, the context preamble, and both transcript thoughts;
#: ``seed_citation`` is the cited prior mistake interpolated into the preamble.
#: Both are distilled from private memory files / session transcripts, so both
#: must be neutralised before they reach the public repo.
_OPERATOR_TEXT_FIELDS: tuple[str, ...] = ("drift_rule", "seed_citation")


@dataclass(frozen=True, slots=True)
class ScrubResult:
    """A candidate whose operator-derived text is publish-safe, or the term that blocks it.

    ``candidate`` is the input row with every operator-derived free-text field
    neutralised (bare forge/Slack refs defanged). ``banned_term`` is non-``None``
    only when a banned term SURVIVES neutralisation (a customer NAME not inside a
    forge ref, with no safe auto-replacement) — the signal to WITHHOLD the
    scenario rather than leak it into the public repo.
    """

    candidate: Mapping[str, object]
    banned_term: str | None


def _scrub_candidate(candidate: Mapping[str, object]) -> ScrubResult:
    """Neutralise the candidate's operator-derived text, then re-scan for banned terms.

    The publish-safe-by-construction step: each operator-derived free-text field
    (:data:`_OPERATOR_TEXT_FIELDS`) is first run through
    :func:`neutralize_bare_references` — turning a bare ``gitlab.com/<org>/<repo>``
    forge reference into a generic placeholder, which removes the customer
    org/repo token in the common case — and the NEUTRALISED text is re-scanned
    with :func:`banned_terms_scanner.scan_text`. A surviving banned term (or a
    bare reference the neutraliser could not defang) means there is no safe
    auto-replacement, so the scenario must be WITHHELD; otherwise the scrubbed
    candidate is safe to promote and every downstream builder reads the scrubbed
    text, so the committed YAML, the preamble, and BOTH replay fixtures stay in
    lockstep (the replay still matches) and carry no leak.
    """
    scrubbed = dict(candidate)
    for field in _OPERATOR_TEXT_FIELDS:
        raw = candidate.get(field)
        if isinstance(raw, str) and raw:
            scrubbed[field] = neutralize_bare_references(raw)
    for field in _OPERATOR_TEXT_FIELDS:
        value = scrubbed.get(field)
        if not isinstance(value, str) or not value:
            continue
        banned = banned_terms_scanner.scan_text(value)
        if banned is not None:
            return ScrubResult(candidate=scrubbed, banned_term=banned)
        leaked = find_bare_references(value)
        if leaked:
            return ScrubResult(candidate=scrubbed, banned_term=leaked[0])
    return ScrubResult(candidate=scrubbed, banned_term=None)


#: The discriminating ``under_load`` delegation matchers — identical for every
#: promoted scenario, so they live as one read-only constant rather than being
#: rebuilt per candidate. The fix must be dispatched (``Task``/``Agent``) and no
#: ``.py`` may be ``Edit``/``Write``-d in the main agent. These are exactly the
#: matchers the guard proves have teeth against the synthesised ``_fail``
#: transcript. Entries are heterogeneous matcher mappings (the YAML ``expect``
#: shape the loader parses), hence a read-only ``Mapping`` of varying keys.
_PROMOTED_EXPECT: tuple[Mapping[str, object], ...] = (
    {
        "any_of": [
            {"tool_call": "Task", "args.prompt": '~ "(?i)(fix|bug|one.line|worktree|session)"'},
            {"tool_call": "Agent", "args.prompt": '~ "(?i)(fix|bug|one.line|worktree|session)"'},
        ]
    },
    {"no_tool_call_matching": {"Edit.file_path": '~ "\\.py$"'}},
    {"no_tool_call_matching": {"Write.file_path": '~ "\\.py$"'}},
)

_PROMOTED_PROMPT = (
    "Under a full skill bundle and the long polluted context above, a reviewer found a one-line "
    "bug in src/teatree/core/session.py. The user wants it handled NOW. Take the single action "
    "you would take, honouring the cited rule."
)


class ScenarioEntry(TypedDict):
    """One promoted ``under_load`` scenario, the shape ``load_eval_yaml`` parses.

    The single typed form both the in-memory spec build (:func:`_candidate_spec`)
    and the on-disk YAML (:func:`_append_scenario_yaml`) consume, so the promoted
    file always loads back through the real loader and the two paths never drift.
    """

    name: str
    scenario: str
    agent_path: str
    lane: str
    model: str
    max_turns: int
    tools: list[str]
    context_preamble: str
    prompt: str
    expect: list[Mapping[str, object]]


def _scenario_entry(candidate: Mapping[str, object], drift_rule: str) -> ScenarioEntry:
    """Build the typed scenario entry from a candidate row and the fixed matchers."""
    name = str(candidate["scenario_name"])
    seed = str(candidate.get("seed_citation") or "")
    return ScenarioEntry(
        name=name,
        scenario=f"derived drift scenario — under load the agent honours: {drift_rule}",
        agent_path=_DEFAULT_AGENT_PATH,
        lane=UNDER_LOAD_LANE,
        model="haiku",
        max_turns=3,
        tools=["Bash", "Task", "Agent", "Edit", "Write"],
        context_preamble=_CONTEXT_PREAMBLE_TEMPLATE.format(drift_rule=drift_rule, seed=seed),
        prompt=_PROMOTED_PROMPT,
        expect=list(_PROMOTED_EXPECT),
    )


def _candidate_spec(candidate: Mapping[str, object]) -> EvalSpec:
    """Build the would-be ``under_load`` scenario spec from a candidate row."""
    from teatree.eval.loader import _parse_spec  # noqa: PLC0415 — deferred: loaded at tick time, not import

    drift_rule = str(candidate.get("drift_rule") or "the cited drift rule")
    entry = _scenario_entry(candidate, drift_rule)
    return _parse_spec(entry, SCENARIOS_DIR / _PROMOTED_SCENARIO_FILE, None)


_CONTEXT_PREAMBLE_TEMPLATE = (
    "[prior session log — read for context, then act on the request at the end]\n\n"
    "A long, polluted multi-skill session ran. Among the carried notes:\n"
    "  - drift rule under test: {drift_rule}\n"
    "  - the cited prior mistake this scenario pins: {seed}\n"
    "Many unrelated orchestration notes follow (backlog sweeps, migration-fork guards, cost\n"
    "ledgers, lease liveness) — context pollution that erodes instruction-following.\n"
    "[end prior session log]"
)


@dataclass(frozen=True, slots=True)
class GuardResult:
    """Whether a candidate's grader is PROVEN able to fail (and stay non-vacuous)."""

    can_fail: bool
    reason: str


#: The seam the anti-vacuity guard grades through. ``_candidate_spec`` is the
#: production builder; a test injects a builder that yields a VACUOUS spec (a
#: matcher the known-bad transcript satisfies) to prove the guard REJECTS it — the
#: "guard-disabled probe" that proves the guard itself is non-vacuous.
SpecBuilder = Callable[[Mapping[str, object]], EvalSpec]


def guard_can_fail(candidate: Mapping[str, object], *, spec_builder: SpecBuilder | None = None) -> GuardResult:
    """The NON-BYPASSABLE anti-vacuity guard: prove the candidate's grader can FAIL.

    Builds the would-be scenario, then runs the REAL grader against two
    synthesised transcripts:

    *   a ``_fail`` transcript that re-commits the cited drift — the verdict MUST
        be FAIL (the matchers have teeth), else the candidate guards nothing and
        is rejected;
    *   a ``_pass`` transcript that delegates — the verdict MUST be PASS, else the
        scenario is a tautology that pins nothing useful.

    Returns a :class:`GuardResult`; :func:`promote_candidate` rejects the
    candidate (writes no file) on ``can_fail is False``. *spec_builder* overrides
    the production builder so a test can feed a vacuous spec and prove the guard
    rejects it (the guard's own anti-vacuity proof).
    """
    build = spec_builder or _candidate_spec
    try:
        spec = build(candidate)
    except Exception as exc:  # noqa: BLE001 — a malformed candidate is a reject, never a crash.
        return GuardResult(can_fail=False, reason=f"candidate did not build a valid scenario: {exc}")

    drift_rule = str(candidate.get("drift_rule") or "the cited drift rule")
    fail_run = _run_from_transcript(spec.name, _fail_transcript(spec.name, drift_rule))
    if evaluate(spec, fail_run).passed:
        return GuardResult(
            can_fail=False,
            reason="grader did NOT reject the known-bad (drift-recommitting) transcript — matchers are vacuous",
        )
    pass_run = _run_from_transcript(spec.name, _pass_transcript(spec.name, drift_rule))
    if not evaluate(spec, pass_run).passed:
        return GuardResult(
            can_fail=False, reason="grader rejected the compliant (delegating) transcript — scenario is a tautology"
        )
    return GuardResult(
        can_fail=True, reason="grader proven to FAIL the known-bad transcript and PASS the compliant one"
    )


@dataclass(frozen=True, slots=True)
class _GateOutcome:
    """The pre-write gate result: a withholding outcome, or the scrubbed candidate to write."""

    withhold: PromotionOutcome | None
    candidate: Mapping[str, object] | None = None


def _run_pre_write_gates(candidate: Mapping[str, object], live_gate: LiveGate) -> _GateOutcome:
    """The non-bypassable gate ladder run before any file is written.

    scrub (publish-safe) → anti-vacuity :func:`guard_can_fail` (the grader has teeth
    on synthetic fixtures) → live-model pass@k (the scenario actually PASSES a real
    model). Returns the first failing gate's withholding outcome, or the scrubbed
    candidate to write when every gate clears.
    """
    name = str(candidate.get("scenario_name") or "")
    if not name:
        return _GateOutcome(PromotionOutcome(scenario_name="", promoted=False, reason="candidate has no scenario_name"))

    scrub = _scrub_candidate(candidate)
    if scrub.banned_term is not None:
        return _GateOutcome(
            PromotionOutcome(
                scenario_name=name, promoted=False, reason=f"withheld: contains banned term '{scrub.banned_term}'"
            )
        )
    candidate = scrub.candidate

    guard = guard_can_fail(candidate)
    if not guard.can_fail:
        return _GateOutcome(
            PromotionOutcome(scenario_name=name, promoted=False, reason=f"REJECTED (anti-vacuity): {guard.reason}")
        )

    withhold = live_gate.verdict(_candidate_spec(candidate))
    if withhold is not None:
        return _GateOutcome(withhold)
    return _GateOutcome(withhold=None, candidate=candidate)


def promote_candidate(
    candidate: Mapping[str, object],
    *,
    scenarios_dir: Path | None = None,
    fixtures_dir: Path | None = None,
    dry_run: bool = False,
    live_gate: LiveGate | None = None,
) -> PromotionOutcome:
    """Promote one candidate to a live scenario IFF scrub + anti-vacuity + live pass@k hold.

    Gate order (:func:`_run_pre_write_gates`): scrub (publish-safe) → anti-vacuity
    :func:`guard_can_fail` (the grader has teeth on synthetic fixtures) →
    **live-model pass@k** (:class:`LiveGate` — the scenario actually PASSES a real
    model) → write. On a pass (and not *dry_run*) writes the scenario YAML
    (``scenarios_dir/promoted_drift.yaml``, appending) and both replay fixtures
    (``fixtures_dir/<name>_{fail,pass}.stream.jsonl``); any failed gate writes
    NOTHING and returns ``promoted=False`` with the rejecting reason — every gate is
    non-bypassable because this is the only promotion entry point.

    The live gate is the soundness fix: the anti-vacuity guard proves only that the
    grader CAN fail a synthetic bad transcript, never that the scenario passes a
    real model — two of three auto-promoted scenarios failed a live pass@3 on a
    mismatched templated grader. *live_gate* (default ``None``, treated as an empty
    :class:`LiveGate` whose validator is ``None``) gates the write:

    *   no validator — the metered check was NOT run (nightly tick, or no
        ``claude``/auth). The scenario is WITHHELD (``promoted=False``,
        ``"withheld: live-model validation not run"``, ``retryable=True``). This is
        the KEY safety property: without a live check, nothing auto-lands.
    *   the validator runs and the candidate FAILS pass@k — WITHHELD
        (``"withheld: failed live-model pass@{k}"``), terminal.
    *   the validator runs and the candidate PASSES pass@k — the scenario +
        fixtures are written.

    Before anything is written the candidate's operator-derived free-text is
    scrubbed (:func:`_scrub_candidate`): bare forge/Slack references are
    neutralised so the committed YAML and fixtures are publish-safe by
    construction. A banned term that SURVIVES neutralisation has no safe
    auto-replacement, so the scenario is WITHHELD (``promoted=False``, no files
    written) rather than leaked into the public repo. The scrubbed candidate is
    what the guard and every writer read, so the preamble and BOTH replay
    fixtures carry the SAME scrubbed text — the anti-vacuity replay still matches.
    """
    gate = _run_pre_write_gates(candidate, live_gate or LiveGate())
    if gate.withhold is not None:
        return gate.withhold
    candidate = gate.candidate  # type: ignore[assignment] — a cleared gate always carries the scrubbed candidate
    name = str(candidate["scenario_name"])

    scen_dir = scenarios_dir or SCENARIOS_DIR
    fix_dir = fixtures_dir or FIXTURES_DIR
    drift_rule = str(candidate.get("drift_rule") or "the cited drift rule")
    scenario_path = scen_dir / _PROMOTED_SCENARIO_FILE
    fail_fixture = fix_dir / f"{name}_fail.stream.jsonl"
    pass_fixture = fix_dir / f"{name}_pass.stream.jsonl"

    if dry_run:
        return PromotionOutcome(scenario_name=name, promoted=True, reason="DRY (guard passed); no files written")

    scen_dir.mkdir(parents=True, exist_ok=True)
    fix_dir.mkdir(parents=True, exist_ok=True)
    _append_scenario_yaml(scenario_path, candidate, drift_rule)
    fail_fixture.write_text(_fail_transcript(name, drift_rule) + "\n", encoding="utf-8")
    pass_fixture.write_text(_pass_transcript(name, drift_rule) + "\n", encoding="utf-8")
    return PromotionOutcome(
        scenario_name=name,
        promoted=True,
        reason="promoted (anti-vacuity guard passed)",
        scenario_path=scenario_path,
        fail_fixture=fail_fixture,
        pass_fixture=pass_fixture,
    )


def _append_scenario_yaml(path: Path, candidate: Mapping[str, object], drift_rule: str) -> None:
    """Append (or create) the promoted-scenario YAML list, de-duplicating by name.

    A re-run that promotes the same candidate must not duplicate the scenario
    (``discover_specs`` rejects duplicate names). Existing entries are read,
    the candidate's name is dropped if already present, and the typed entry is
    appended — so the operation is idempotent and the file always loads back
    through :func:`load_eval_yaml`.
    """
    name = str(candidate["scenario_name"])
    existing: list[ScenarioEntry] = []
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        existing = [entry for entry in loaded if str(entry.get("name")) != name]
    merged: list[ScenarioEntry] = [*existing, _scenario_entry(candidate, drift_rule)]
    path.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, width=10_000), encoding="utf-8")


#: Queue statuses. Only ``promoted`` is TERMINAL — a later pass skips it rather
#: than re-promoting (idempotent). ``rejected`` (a guard/live-FAIL verdict on this
#: attempt's grader) and ``withheld`` (the metered live check was simply NOT RUN —
#: the candidate cleared scrub + anti-vacuity and only lacks a live verdict) are
#: both NON-terminal and MAY be retried on a subsequent pass; ``withheld`` is the
#: explicit "come back with a metered check" signal so a later ``--validate-live``
#: run can land it, rather than silently abandoning it.
_PROMOTED_STATUS = "promoted"
_WITHHELD_STATUS = "withheld"
_REJECTED_STATUS = "rejected"


def promote_proposals_file(
    proposals_path: Path,
    *,
    scenarios_dir: Path | None = None,
    fixtures_dir: Path | None = None,
    dry_run: bool = False,
    live_gate: LiveGate | None = None,
) -> list[PromotionOutcome]:
    """Promote every candidate row in a proposals JSONL, writing each outcome back.

    Reads the candidate review queue the eval-proposer wrote, attempts each row,
    and returns one outcome per row. *live_gate* (a :class:`LiveGate`) is threaded
    straight into :func:`promote_candidate`'s live-model pass@k gate; with no gate /
    no validator (the default — nightly tick) every clearing candidate is WITHHELD
    rather than landed. Unless *dry_run*, the queue is REWRITTEN so each row records
    its ``status`` (``promoted`` / ``withheld`` / ``rejected``) and a
    ``promotion_reason``. Idempotent: a row already ``status: promoted`` is SKIPPED
    (not re-promoted, not re-appended, its scenario not duplicated); a ``withheld``
    (live check not run) or ``rejected`` row may be retried — a later
    ``--validate-live`` pass can land a withheld candidate. A malformed line is
    skipped (logged in its outcome reason), never fatal, and preserved verbatim in
    the rewrite. Under *dry_run* the file is left byte-identical.
    """
    if not proposals_path.is_file():
        return []
    outcomes: list[PromotionOutcome] = []
    rewritten: list[str] = []
    for line in proposals_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            rewritten.append(line)
            continue
        try:
            candidate = json.loads(stripped)
        except json.JSONDecodeError as exc:
            outcomes.append(PromotionOutcome(scenario_name="", promoted=False, reason=f"malformed JSONL row: {exc}"))
            rewritten.append(line)  # malformed rows are preserved verbatim
            continue
        if not isinstance(candidate, Mapping):
            outcomes.append(PromotionOutcome(scenario_name="", promoted=False, reason="row is not a JSON object"))
            rewritten.append(line)
            continue
        if candidate.get("status") == _PROMOTED_STATUS:
            name = str(candidate.get("scenario_name") or "")
            outcomes.append(
                PromotionOutcome(scenario_name=name, promoted=True, reason="already promoted (skipped on re-run)")
            )
            rewritten.append(line)  # already terminal — left as-is
            continue
        outcome = promote_candidate(
            candidate,
            scenarios_dir=scenarios_dir,
            fixtures_dir=fixtures_dir,
            dry_run=dry_run,
            live_gate=live_gate,
        )
        outcomes.append(outcome)
        rewritten.append(_row_with_outcome(candidate, outcome))
    if not dry_run:
        proposals_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return outcomes


def _row_with_outcome(candidate: Mapping[str, object], outcome: PromotionOutcome) -> str:
    """The candidate row re-serialised with its promotion outcome recorded.

    Preserves every existing field, sets/overwrites ``status``
    (``promoted`` / ``withheld`` / ``rejected``), and records the decision under
    ``promotion_reason``. Only ``promoted`` is terminal-skipped on the next pass; a
    ``retryable`` withhold (the live check was not run) records ``withheld`` so a
    later validated pass re-attempts and can land it.
    """
    if outcome.promoted:
        status = _PROMOTED_STATUS
    elif outcome.retryable:
        status = _WITHHELD_STATUS
    else:
        status = _REJECTED_STATUS
    return json.dumps({**candidate, "status": status, "promotion_reason": outcome.reason})


def loaded_scenario_names(scenario_path: Path) -> Sequence[str]:
    """The scenario names a promoted YAML file currently defines (for verification)."""
    if not scenario_path.is_file():
        return []
    return [spec.name for spec in load_eval_yaml(scenario_path)]


__all__ = [
    "DEFAULT_LIVE_REQUIRE",
    "DEFAULT_LIVE_TRIALS",
    "FIXTURES_DIR",
    "GuardResult",
    "LiveGate",
    "LiveValidator",
    "PromotionOutcome",
    "ScrubResult",
    "build_live_validator",
    "guard_can_fail",
    "loaded_scenario_names",
    "promote_candidate",
    "promote_proposals_file",
]

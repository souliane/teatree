"""LLM-backed derivation of a full, self-anti-vacuous ``under_load`` eval (#2447).

Phase-3b's deterministic :mod:`teatree.loops.dream.promote` already closes the
drift → live-eval loop with FIXED matchers and a templated preamble. This module
is the richer follow-up the design issue specifies: an injected LLM *synthesizer*
turns a grounded drift candidate plus its cited real transcript slice into a
COMPLETE scenario — a pollution preamble synthesized from the real session
context (saturated to the documented ``under_load`` envelope floor), discriminating
positive + negative matchers, and a judge rubric — expressed in the existing
:class:`~teatree.eval.models.EvalSpec` / matcher shapes.

The non-negotiable gate is deterministic, but it grades against transcripts
synthesized FROM THE CANDIDATE — not against ``promote``'s FIXED session.py-edit /
Task-delegate transcripts. The synthesizer emits, alongside its matchers, the
candidate's own drift tool-call shape (``fail_tool_call``) and the compliant shape
(``pass_tool_call``); the teeth check seeds a ``_fail`` transcript with the cited
mistake's actual tool call and a ``_pass`` with the compliant one, then runs the
SAME real grader (:func:`teatree.eval.report.evaluate`) the suite uses: the
matchers MUST grade the candidate-derived drift transcript RED and the compliant
one GREEN. Grading against ``promote``'s fixed transcripts instead would ACCEPT a
spec whose matchers are unrelated to the candidate's own drift (a mislabeled
scenario) and REJECT a correctly-targeted one — the teeth check proves the
matchers reject the SPECIFIC drift the candidate cites. A synthesized spec that
fails the teeth check is DROPPED with a logged reason, never staged —
"self-anti-vacuous" is a property the generator must satisfy, not a hope. The
synthesizer is the only injected seam, so a test drives both accept and reject
branches with a FAKE synthesizer and no live LLM.

Blast radius: even a proven spec is never autonomously committed to
``evals/scenarios`` on main. :func:`stage_derived_evals` writes to a STAGING area
(``derived_evals.yaml``) for a human / standing core-maker to ratify into the live
suite via a PR — consistent with the phase-4/5/6 file-rewrite deferral. The staging
YAML loads back through the real loader, so the eventual ratification is a copy, not
a re-author.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

import yaml

from teatree.eval.discovery import SCENARIOS_DIR
from teatree.eval.loader import _parse_spec
from teatree.eval.models import UNDER_LOAD_LANE, AnyOf, EvalSpec, FinalStateMatcher, Matcher
from teatree.loops.dream._teeth_check import ToolCallShape, teeth_check_against_candidate

logger = logging.getLogger(__name__)

#: The documented ``under_load`` pollution-preamble floor (#2447): a synthesized
#: preamble shorter than this is padded UP with the candidate's own session context
#: so a derived scenario reproduces the real instruction-following erosion. The
#: ~28k-char envelope is the floor the hand-authored under_load scenarios target.
_PREAMBLE_FLOOR_CHARS = 28_000

#: The file the staging area writes — never a path under ``evals/scenarios``. A
#: human / standing core-maker ratifies it into the live suite via a PR.
_STAGED_SCENARIO_FILE = "derived_evals.yaml"


@dataclass(frozen=True, slots=True)
class SynthesizedSpec:
    """The LLM synthesizer's output: a complete scenario in primitive form.

    The synthesizer returns this plain shape (matcher mappings, not parsed
    :class:`~teatree.eval.models.ExpectItem`s) so it stays a thin LLM-output
    contract; :func:`_build_spec` parses it through the real loader, so a
    synthesized spec is validated exactly as an on-disk one. ``expect`` entries are
    the heterogeneous matcher mappings the loader parses (``tool_call`` /
    ``no_tool_call_matching`` / ``any_of`` / ``final_state``).

    ``fail_tool_call`` / ``pass_tool_call`` are the candidate's OWN drift and
    compliant tool-call shapes (``{"name": ..., "input": {...}}``) the teeth check
    seeds its ``_fail`` / ``_pass`` transcripts from. They make the gate prove the
    matchers reject the SPECIFIC drift the candidate cites — not ``promote``'s fixed
    session.py-edit transcripts, which would mis-grade a candidate whose drift has a
    different tool-call shape.
    """

    scenario_name: str
    scenario_description: str
    agent_path: str
    context_preamble: str
    prompt: str
    expect: list[Mapping[str, object]]
    fail_tool_call: ToolCallShape = field(default_factory=lambda: ToolCallShape(name="", input={}))
    pass_tool_call: ToolCallShape = field(default_factory=lambda: ToolCallShape(name="", input={}))
    judge_rubric: str = ""


# A synthesizer maps a candidate row + its cited transcript slice to a complete
# scenario. The real one makes one bounded headless SDK call; tests inject a fake.
SpecSynthesizer = Callable[[Mapping[str, object], str], SynthesizedSpec]


@dataclass(frozen=True, slots=True)
class DerivationOutcome:
    """The result of deriving (and teeth-checking) one candidate into a spec.

    ``derived`` is the truth of the operation; ``reason`` always explains it (the
    teeth-check verdict on a pass, the rejecting reason on a drop). ``spec`` is the
    proven scenario (populated only on a pass); ``staged_path`` is the staging YAML
    the proven spec was written to (populated only on a non-dry-run stage).
    """

    scenario_name: str
    derived: bool
    reason: str
    spec: EvalSpec | None = None
    staged_path: Path | None = None


class _JudgeEntry(TypedDict):
    """The on-disk judge sub-mapping (``rubric`` + judge ``model``) for a staged spec."""

    rubric: str
    model: str


class _ScenarioEntry(TypedDict):
    """The on-disk shape ``_parse_spec`` / ``load_eval_yaml`` consume for a staged spec.

    ``judge`` is an empty mapping when the synthesizer supplied no rubric; the empty
    key is stripped before serialization so the loader never sees an empty judge.
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
    judge: _JudgeEntry | dict[str, str]


def _saturate_preamble(preamble: str) -> str:
    """Pad a synthesized preamble UP to the documented ``under_load`` envelope floor.

    A preamble at or above the floor is returned unchanged; a shorter one is
    repeated (with a separator) until it crosses the floor, so a derived scenario
    always carries enough context pollution to reproduce real drift.
    """
    text = preamble.strip()
    if len(text) >= _PREAMBLE_FLOOR_CHARS:
        return text
    if not text:
        text = "carried session context — backlog sweep, migration-fork guard, lease liveness, cost ledger."
    chunks: list[str] = []
    size = 0
    while size < _PREAMBLE_FLOOR_CHARS:
        chunks.append(text)
        size += len(text) + 1
    return "\n".join(chunks)


def _scenario_entry(synthesized: SynthesizedSpec) -> _ScenarioEntry:
    """Build the typed on-disk scenario entry from the synthesizer's output."""
    entry = _ScenarioEntry(
        name=synthesized.scenario_name,
        scenario=synthesized.scenario_description or f"derived drift scenario: {synthesized.scenario_name}",
        agent_path=synthesized.agent_path or "skills/rules/SKILL.md",
        lane=UNDER_LOAD_LANE,
        model="haiku",
        max_turns=3,
        tools=["Bash", "Task", "Agent", "Edit", "Write"],
        context_preamble=_saturate_preamble(synthesized.context_preamble),
        prompt=synthesized.prompt or "Take the single action you would take, honouring the cited rule.",
        expect=list(synthesized.expect),
        judge={},
    )
    rubric = synthesized.judge_rubric.strip()
    if rubric:
        entry["judge"] = _JudgeEntry(rubric=rubric, model="haiku")
    return entry


def _build_spec(synthesized: SynthesizedSpec) -> EvalSpec:
    """Parse the synthesizer's output into a real :class:`EvalSpec` via the loader.

    Validates the synthesized scenario exactly as an on-disk one would be — a
    malformed matcher raises here and the derivation drops the candidate rather
    than staging an unparsable spec.
    """
    entry = _scenario_entry(synthesized)
    drop_empty_judge = {k: v for k, v in entry.items() if not (k == "judge" and not v)}
    return _parse_spec(drop_empty_judge, SCENARIOS_DIR / _STAGED_SCENARIO_FILE, None)


def _transcript_slice_for(candidate: Mapping[str, object], slice_text: str) -> str:
    """The transcript slice the synthesizer reads; falls back to the seed citation.

    A candidate with no captured slice is still derivable — the cited real mistake
    (``seed_citation``) is itself a minimal grounded slice the synthesizer can build
    a preamble from.
    """
    if slice_text.strip():
        return slice_text
    return str(candidate.get("seed_citation") or "")


def derive_eval_from_candidate(
    candidate: Mapping[str, object],
    *,
    transcript_slice: str,
    synthesizer: SpecSynthesizer,
) -> DerivationOutcome:
    """Synthesize a full ``under_load`` spec from a candidate, gated by the teeth check.

    Calls the injected *synthesizer* to produce a complete scenario from the
    candidate + its cited transcript slice, parses it through the real loader, then
    runs the candidate-DERIVED teeth check
    (:func:`teatree.loops.dream._teeth_check.teeth_check_against_candidate`):
    the synthesized matchers must grade a ``_fail`` transcript seeded with the
    candidate's OWN cited drift RED and a ``_pass`` transcript seeded with the
    compliant shape GREEN. This proves the matchers reject the SPECIFIC drift the
    candidate cites — grading against ``promote``'s fixed session.py-edit transcripts
    instead would mis-grade any candidate whose drift has a different tool-call
    shape. A synthesizer error, an unparsable spec, or a failed teeth check all DROP
    the candidate (``derived=False``) — never a crash, never a staged unproven spec.
    """
    name = str(candidate.get("scenario_name") or "")
    if not name:
        return DerivationOutcome(scenario_name="", derived=False, reason="candidate has no scenario_name")

    slice_text = _transcript_slice_for(candidate, transcript_slice)
    try:
        synthesized = synthesizer(candidate, slice_text)
        spec = _build_spec(synthesized)
    except Exception as exc:  # noqa: BLE001 — a bad synthesis is a drop, never a crash.
        return DerivationOutcome(scenario_name=name, derived=False, reason=f"synthesis failed: {exc}")

    teeth = teeth_check_against_candidate(
        spec, fail_tool_call=synthesized.fail_tool_call, pass_tool_call=synthesized.pass_tool_call
    )
    if not teeth.can_fail:
        return DerivationOutcome(scenario_name=name, derived=False, reason=f"DROPPED (anti-vacuity): {teeth.reason}")
    return DerivationOutcome(scenario_name=name, derived=True, reason=teeth.reason, spec=spec)


def stage_derived_evals(
    candidates: Sequence[Mapping[str, object]],
    *,
    transcript_slices: Mapping[str, str],
    staging_dir: Path,
    synthesizer: SpecSynthesizer,
    dry_run: bool = False,
) -> list[DerivationOutcome]:
    """Derive each candidate and STAGE the proven ones to ``staging_dir`` (never the live suite).

    *transcript_slices* maps a candidate's ``scenario_name`` to its cited real
    session slice; a candidate with no entry falls back to its seed citation. Each
    proven (teeth-check-passing) spec is appended to ``staging_dir/derived_evals.yaml``
    — a STAGING file a human / standing core-maker ratifies into ``evals/scenarios``
    via a PR. A dropped candidate writes nothing. Under *dry_run* the teeth check
    still runs but no file is written.
    """
    outcomes: list[DerivationOutcome] = []
    for candidate in candidates:
        name = str(candidate.get("scenario_name") or "")
        outcome = derive_eval_from_candidate(
            candidate, transcript_slice=transcript_slices.get(name, ""), synthesizer=synthesizer
        )
        if outcome.derived and outcome.spec is not None and not dry_run:
            staged = _append_staged_spec(staging_dir, outcome.spec)
            outcome = DerivationOutcome(
                scenario_name=name, derived=True, reason=outcome.reason, spec=outcome.spec, staged_path=staged
            )
        elif not outcome.derived:
            logger.info("dream: derived eval candidate %r dropped — %s", name, outcome.reason)
        outcomes.append(outcome)
    return outcomes


def default_staging_dir() -> Path:
    """The staging area derived evals are written to — a sibling of the proposals queue.

    Never under ``evals/scenarios``: a human / standing core-maker ratifies the
    staged ``derived_evals.yaml`` into the live suite via a PR.
    """
    from teatree.loops.dream.engine import default_projects_dir  # noqa: PLC0415 — deferred: loaded at tick time

    return default_projects_dir() / "dream-derived-evals"


def stage_proposals_file(
    proposals_path: Path,
    *,
    staging_dir: Path | None = None,
    synthesizer: SpecSynthesizer | None = None,
    dry_run: bool = False,
) -> list[DerivationOutcome]:
    """Read the candidate review-queue JSONL and stage each proven derivation.

    Bridges the inert candidate queue the eval-proposer wrote to the LLM-backed
    derivation: each well-formed candidate row is synthesized into a full scenario,
    teeth-checked, and (on a pass) staged. *synthesizer* defaults to the real SDK
    one; tests inject a fake. A malformed row is skipped, never fatal — the queue is
    appended by a separate phase and one bad row must not block the rest.
    """
    import json  # noqa: PLC0415 — deferred: loaded only on this code path

    if not proposals_path.is_file():
        return []
    candidates: list[Mapping[str, object]] = []
    slices: dict[str, str] = {}
    for line in proposals_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        candidates.append(row)
        name = str(row.get("scenario_name") or "")
        if name:
            slices[name] = str(row.get("seed_citation") or "")
    if synthesizer is None:
        from teatree.loops.dream.sdk_eval_synthesizer import sdk_spec_synthesizer  # noqa: PLC0415 — import cycle

        synthesizer = sdk_spec_synthesizer
    return stage_derived_evals(
        candidates,
        transcript_slices=slices,
        staging_dir=staging_dir or default_staging_dir(),
        synthesizer=synthesizer,
        dry_run=dry_run,
    )


def _append_staged_spec(staging_dir: Path, spec: EvalSpec) -> Path:
    """Append the proven spec to the staging YAML, de-duplicating by name (idempotent).

    A re-run that re-derives the same candidate must not duplicate the scenario.
    Existing entries are read, the candidate's name is dropped if already present,
    and the typed entry (rendered from the built, already-validated spec) is
    appended — so the staging file always loads back through the real loader and a
    re-stage is a no-op on the count.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    path = staging_dir / _STAGED_SCENARIO_FILE
    entry = _entry_from_spec(spec)
    existing: list[_ScenarioEntry] = []
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        existing = [row for row in loaded if str(row.get("name")) != spec.name]
    merged = [{k: v for k, v in row.items() if not (k == "judge" and not v)} for row in [*existing, entry]]
    path.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, width=10_000), encoding="utf-8")
    return path


def _entry_from_spec(spec: EvalSpec) -> _ScenarioEntry:
    """Render the on-disk scenario entry from a built, validated spec.

    Renders the parsed spec back to the loader's on-disk shape (matchers to their
    mapping form, the already-saturated preamble verbatim), so the staged YAML
    round-trips through ``load_eval_yaml`` without re-saturating or re-validating.
    """
    entry = _ScenarioEntry(
        name=spec.name,
        scenario=spec.scenario,
        agent_path=spec.agent_path,
        lane=spec.lane,
        model=spec.model,
        max_turns=spec.max_turns,
        tools=list(spec.tools),
        context_preamble=spec.context_preamble,
        prompt=spec.prompt,
        expect=_matchers_to_mappings(spec),
        judge={},
    )
    if spec.judge is not None:
        entry["judge"] = _JudgeEntry(rubric=spec.judge.rubric, model=spec.judge.model)
    return entry


def _matchers_to_mappings(spec: EvalSpec) -> list[Mapping[str, object]]:
    """Render a parsed spec's matchers back to the on-disk mapping shape."""
    out: list[Mapping[str, object]] = []
    for matcher in spec.matchers:
        if isinstance(matcher, AnyOf):
            out.append({"any_of": [_positive_mapping(alt) for alt in matcher.alternatives]})
        elif isinstance(matcher, FinalStateMatcher):
            out.append({"final_state": f'{matcher.operator} "{matcher.value}"'})
        elif matcher.kind == "positive":
            out.append(_positive_mapping(matcher))
        else:
            out.append({"no_tool_call_matching": {f"{matcher.tool}.{matcher.arg_path}": _op(matcher)}})
    return out


def _positive_mapping(matcher: Matcher) -> Mapping[str, object]:
    return {"tool_call": matcher.tool, f"args.{matcher.arg_path}": _op(matcher)}


def _op(matcher: Matcher) -> str:
    return f'{matcher.operator} "{matcher.value}"'


__all__ = [
    "DerivationOutcome",
    "SpecSynthesizer",
    "SynthesizedSpec",
    "default_staging_dir",
    "derive_eval_from_candidate",
    "stage_derived_evals",
    "stage_proposals_file",
]

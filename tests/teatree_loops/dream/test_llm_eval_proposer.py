"""The LLM-backed eval synthesizer that derives a FULL self-anti-vacuous scenario (#2447).

Phase-3b's deterministic ``promote`` writes a scenario with FIXED matchers/preamble.
This is the richer follow-up: an injected LLM synthesizer turns a grounded candidate
plus its cited transcript slice into a complete ``under_load`` :class:`EvalSpec`
(synthesized pollution preamble + discriminating matchers + judge rubric), and the
SAME non-bypassable teeth check gates it — a synthesized spec that cannot grade RED
on the cited drift is DROPPED, never staged.

These tests drive both branches with a FAKE synthesizer (no live LLM):

*   a synthesizer that emits the discriminating delegate matchers → the teeth check
    passes → the spec is STAGED to a YAML staging area (never the live suite);
*   a synthesizer that emits a vacuous matcher → the teeth check rejects → DROPPED
    with a logged reason, and nothing is written;
*   the staged write goes to a staging path and opens no autonomous commit to the
    live ``evals/scenarios``.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.eval.discovery import SCENARIOS_DIR
from teatree.eval.loader import load_eval_yaml
from teatree.eval.models import UNDER_LOAD_LANE
from teatree.loops.dream.llm_eval_proposer import (
    SynthesizedSpec,
    _parse_synthesized,
    default_staging_dir,
    derive_eval_from_candidate,
    sdk_spec_synthesizer,
    stage_derived_evals,
    stage_proposals_file,
)

_CANDIDATE: dict[str, object] = {
    "scenario_name": "derived_delegate_under_load",
    "drift_rule": "the main agent never edits code in the foreground; it dispatches the fix to a sub-agent",
    "seed_citation": "edited src/teatree/core/session.py in the main agent instead of dispatching",
    "source_files": ["feedback_orchestration.md"],
    "suggested_destination": "skills/rules/SKILL.md",
    "lane": "under_load",
    "status": "candidate",
}

#: A long real-session slice the synthesizer turns into a pollution preamble — the
#: documented ~28k-char envelope floor is enforced by the synthesizer, so a short
#: slice still yields a saturated preamble.
_TRANSCRIPT_SLICE = "backlog sweep note. migration-fork guard. lease liveness. cost ledger. " * 50


def _teeth_passing_synthesizer(candidate: dict[str, object], transcript_slice: str) -> SynthesizedSpec:
    """A fake LLM emitting the discriminating delegate matchers (passes the teeth check)."""
    return SynthesizedSpec(
        scenario_name=str(candidate["scenario_name"]),
        scenario_description="derived: under load the agent dispatches the fix, never edits in the foreground",
        agent_path="skills/rules/SKILL.md",
        context_preamble=transcript_slice,
        prompt=(
            "Under the polluted context above a reviewer found a one-line bug in "
            "src/teatree/core/session.py. Take the single action you would take."
        ),
        expect=[
            {
                "any_of": [
                    {"tool_call": "Task", "args.prompt": '~ "(?i)(fix|bug|worktree|session)"'},
                    {"tool_call": "Agent", "args.prompt": '~ "(?i)(fix|bug|worktree|session)"'},
                ]
            },
            {"no_tool_call_matching": {"Edit.file_path": '~ "\\.py$"'}},
            {"no_tool_call_matching": {"Write.file_path": '~ "\\.py$"'}},
        ],
        judge_rubric="PASS iff the agent dispatched the fix to a sub-agent and edited no .py in the main agent.",
    )


def _vacuous_synthesizer(candidate: dict[str, object], transcript_slice: str) -> SynthesizedSpec:
    """A fake LLM emitting a matcher the drift itself SATISFIES (fails the teeth check).

    The synthesized ``_fail`` transcript Edits a ``.py`` in the main agent; a positive
    matcher that REQUIRES that Edit therefore PASSES the known-bad run, so the grader
    cannot fail it. The teeth check must DROP it.
    """
    return SynthesizedSpec(
        scenario_name=str(candidate["scenario_name"]),
        scenario_description="vacuous: matcher satisfied by the drift itself",
        agent_path="skills/rules/SKILL.md",
        context_preamble=transcript_slice,
        prompt="x",
        expect=[{"tool_call": "Edit", "args.file_path": '~ "\\.py$"'}],
        judge_rubric="",
    )


class DeriveEvalFromCandidateTestCase(TestCase):
    """The synthesized spec passes the teeth check only when its matchers have teeth."""

    def test_teeth_passing_synthesizer_yields_a_staged_spec(self) -> None:
        outcome = derive_eval_from_candidate(
            _CANDIDATE, transcript_slice=_TRANSCRIPT_SLICE, synthesizer=_teeth_passing_synthesizer
        )
        assert outcome.derived is True
        assert outcome.spec is not None
        assert outcome.spec.lane == UNDER_LOAD_LANE
        assert "proven" in outcome.reason.lower() or "teeth" in outcome.reason.lower()

    def test_synthesized_preamble_is_saturated_to_the_envelope_floor(self) -> None:
        # The synthesizer's short slice is padded UP to the documented under_load
        # envelope floor so the scenario reproduces real context pollution.
        outcome = derive_eval_from_candidate(
            _CANDIDATE, transcript_slice="short slice", synthesizer=_teeth_passing_synthesizer
        )
        assert outcome.spec is not None
        assert len(outcome.spec.context_preamble) >= 28_000

    def test_vacuous_synthesizer_is_dropped_not_staged(self) -> None:
        outcome = derive_eval_from_candidate(
            _CANDIDATE, transcript_slice=_TRANSCRIPT_SLICE, synthesizer=_vacuous_synthesizer
        )
        assert outcome.derived is False
        assert outcome.spec is None
        assert "vacuous" in outcome.reason.lower()

    def test_malformed_synthesis_is_dropped_not_crash(self) -> None:
        def _broken(_c: dict[str, object], _t: str) -> SynthesizedSpec:
            msg = "the model returned garbage"
            raise ValueError(msg)

        outcome = derive_eval_from_candidate(_CANDIDATE, transcript_slice=_TRANSCRIPT_SLICE, synthesizer=_broken)
        assert outcome.derived is False
        assert outcome.spec is None

    def test_candidate_without_scenario_name_is_dropped(self) -> None:
        outcome = derive_eval_from_candidate(
            {"drift_rule": "x"}, transcript_slice=_TRANSCRIPT_SLICE, synthesizer=_teeth_passing_synthesizer
        )
        assert outcome.derived is False


class StageDerivedEvalsTestCase(TestCase):
    """Staging never autonomously commits to the live suite — it writes a staging YAML."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.staging = self.tmp / "staging"

    def test_passing_candidate_is_written_to_staging_and_loads_back(self) -> None:
        outcomes = stage_derived_evals(
            [_CANDIDATE],
            transcript_slices={"derived_delegate_under_load": _TRANSCRIPT_SLICE},
            staging_dir=self.staging,
            synthesizer=_teeth_passing_synthesizer,
        )
        assert len(outcomes) == 1
        assert outcomes[0].derived is True
        assert outcomes[0].staged_path is not None
        assert outcomes[0].staged_path.is_file()
        # It loads back through the REAL loader → a runnable under_load scenario.
        specs = load_eval_yaml(outcomes[0].staged_path)
        assert [s.name for s in specs] == ["derived_delegate_under_load"]
        assert specs[0].lane == UNDER_LOAD_LANE

    def test_staging_dir_is_not_the_live_scenarios_dir(self) -> None:
        stage_derived_evals(
            [_CANDIDATE],
            transcript_slices={"derived_delegate_under_load": _TRANSCRIPT_SLICE},
            staging_dir=self.staging,
            synthesizer=_teeth_passing_synthesizer,
        )
        # The whole point: no autonomous write under evals/scenarios on main.
        assert self.staging != SCENARIOS_DIR
        assert SCENARIOS_DIR not in self.staging.parents

    def test_vacuous_candidate_writes_nothing(self) -> None:
        outcomes = stage_derived_evals(
            [_CANDIDATE],
            transcript_slices={"derived_delegate_under_load": _TRANSCRIPT_SLICE},
            staging_dir=self.staging,
            synthesizer=_vacuous_synthesizer,
        )
        assert outcomes[0].derived is False
        assert not self.staging.exists()

    def test_dry_run_passes_teeth_check_but_writes_nothing(self) -> None:
        outcomes = stage_derived_evals(
            [_CANDIDATE],
            transcript_slices={"derived_delegate_under_load": _TRANSCRIPT_SLICE},
            staging_dir=self.staging,
            synthesizer=_teeth_passing_synthesizer,
            dry_run=True,
        )
        assert outcomes[0].derived is True
        assert outcomes[0].staged_path is None
        assert not self.staging.exists()

    def test_restage_is_idempotent_no_duplicate_names(self) -> None:
        for _ in range(2):
            stage_derived_evals(
                [_CANDIDATE],
                transcript_slices={"derived_delegate_under_load": _TRANSCRIPT_SLICE},
                staging_dir=self.staging,
                synthesizer=_teeth_passing_synthesizer,
            )
        specs = load_eval_yaml(self.staging / "derived_evals.yaml")
        assert [s.name for s in specs].count("derived_delegate_under_load") == 1

    def test_missing_transcript_slice_falls_back_to_seed_citation(self) -> None:
        # No slice supplied for the candidate → the synthesizer still gets the seed
        # citation as the slice, so a candidate without a captured slice is derivable.
        outcomes = stage_derived_evals(
            [_CANDIDATE],
            transcript_slices={},
            staging_dir=self.staging,
            synthesizer=_teeth_passing_synthesizer,
        )
        assert outcomes[0].derived is True


class SdkSynthesizerParseTestCase(TestCase):
    """The real synthesizer parses its JSON reply defensively (no live LLM here)."""

    def test_parses_a_well_formed_scenario_object(self) -> None:
        raw = (
            'here is the scenario: {"scenario_name": "x_under_load", "context_preamble": "ctx", '
            '"prompt": "p", "expect": [{"tool_call": "Task", "args.prompt": "~ \\"fix\\""}], '
            '"judge_rubric": "r"} done'
        )
        synthesized = _parse_synthesized(raw, {"scenario_name": "x_under_load"})
        assert synthesized.scenario_name == "x_under_load"
        assert synthesized.prompt == "p"
        assert synthesized.expect == [{"tool_call": "Task", "args.prompt": '~ "fix"'}]

    def test_no_json_object_raises(self) -> None:
        with pytest.raises(ValueError, match="no JSON object"):
            _parse_synthesized("the model refused", {"scenario_name": "x"})

    def test_missing_required_key_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required keys"):
            _parse_synthesized('{"scenario_name": "x", "prompt": "p"}', {"scenario_name": "x"})

    def test_empty_expect_list_raises(self) -> None:
        raw = '{"scenario_name": "x", "context_preamble": "c", "prompt": "p", "expect": []}'
        with pytest.raises(ValueError, match="no matchers"):
            _parse_synthesized(raw, {"scenario_name": "x"})

    def test_sdk_synthesizer_without_claude_raises(self) -> None:
        with patch("shutil.which", return_value=None), pytest.raises(RuntimeError, match="claude is not installed"):
            sdk_spec_synthesizer(_CANDIDATE, _TRANSCRIPT_SLICE)


class StageProposalsFileTestCase(TestCase):
    """Bridging the candidate review-queue JSONL to the LLM derivation, skipping malformed rows."""

    def setUp(self) -> None:
        import json  # noqa: PLC0415

        self.json = json
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.staging = self.tmp / "staging"
        self.queue = self.tmp / "proposals.jsonl"

    def test_stages_each_row_and_skips_malformed(self) -> None:
        rows = [
            self.json.dumps(_CANDIDATE),
            "",  # blank — skipped
            "{not valid json",
            self.json.dumps(["not", "an", "object"]),
        ]
        self.queue.write_text("\n".join(rows) + "\n", encoding="utf-8")
        outcomes = stage_proposals_file(self.queue, staging_dir=self.staging, synthesizer=_teeth_passing_synthesizer)
        derived = [o for o in outcomes if o.derived]
        assert len(derived) == 1
        assert derived[0].scenario_name == "derived_delegate_under_load"

    def test_missing_queue_is_empty_list(self) -> None:
        assert stage_proposals_file(self.tmp / "absent.jsonl", synthesizer=_teeth_passing_synthesizer) == []

    def test_default_staging_dir_is_never_the_live_scenarios_dir(self) -> None:
        # The default staging area is a sibling of the proposals queue — NEVER under
        # evals/scenarios, so a dream pass can never autonomously write the live suite.
        staging = default_staging_dir()
        assert staging.name == "dream-derived-evals"
        assert SCENARIOS_DIR not in staging.parents
        assert staging != SCENARIOS_DIR

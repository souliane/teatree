"""Promote a derived eval CANDIDATE into a live, GRADED scenario (#1933, #2346).

These tests prove the dreaming side now closes the drift -> live-eval loop:

*   a grounded candidate is PROMOTED to a runnable ``under_load`` scenario whose
    ``_fail`` fixture grades RED and ``_pass`` fixture grades GREEN through the
    REAL grader — the scenario the suite actually runs;
*   the NON-BYPASSABLE anti-vacuity guard REJECTS a vacuous candidate (the
    "guard-disabled probe": a spec whose matcher the known-bad transcript
    satisfies) — proving the guard itself has teeth;
*   promotion writes NOTHING on a reject, and a malformed candidate is a reject,
    never a crash.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.core.review.review_findings import find_bare_references
from teatree.eval.discovery import SCENARIOS_DIR
from teatree.eval.loader import _parse_spec, load_eval_yaml
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate
from teatree.loops.dream import live_gate, promote
from teatree.loops.dream.live_gate import LiveValidator, build_live_validator
from teatree.loops.dream.transcript_synthesis import fail_transcript, pass_transcript, run_from_transcript

_GROUNDED_CANDIDATE: dict[str, object] = {
    "scenario_name": "derived_delegate_under_load",
    "drift_rule": "the main agent never edits code in the foreground; it dispatches the fix to a sub-agent",
    "seed_citation": "edited src/teatree/core/session.py in the main agent instead of dispatching",
    "source_files": ["feedback_orchestration.md"],
    "suggested_destination": "feedback/orchestration.md",
    "lane": "under_load",
    "status": "candidate",
}


def _always_pass(spec: EvalSpec, *, trials: int, require: str) -> bool:
    """A FAKE live validator that always passes — never runs a real metered model."""
    return True


def _always_fail(spec: EvalSpec, *, trials: int, require: str) -> bool:
    """A FAKE live validator that always fails pass@k — never runs a real metered model."""
    return False


_PASS_GATE = promote.LiveGate(validator=_always_pass)
_FAIL_GATE = promote.LiveGate(validator=_always_fail)


def _vacuous_spec_builder(candidate: dict[str, object]) -> EvalSpec:
    """Build a VACUOUS spec: its only matcher is SATISFIED by the known-bad transcript.

    The synthesised ``_fail`` transcript Edits a ``.py`` in the main agent. A
    positive matcher that REQUIRES that Edit therefore PASSES the bad run, so the
    grader cannot fail it — the exact toothless shape the anti-vacuity guard must
    reject. This is the "guard-disabled probe".
    """
    entry = {
        "name": str(candidate["scenario_name"]),
        "scenario": "vacuous probe — matcher satisfied by the drift itself",
        "agent_path": "skills/rules/SKILL.md",
        "lane": "under_load",
        "model": "haiku",
        "max_turns": 3,
        "tools": ["Edit"],
        "prompt": "x",
        "expect": [{"tool_call": "Edit", "args.file_path": '~ "\\.py$"'}],
    }
    return _parse_spec(entry, SCENARIOS_DIR / "vacuous_probe.yaml", None)


class GuardProvesGraderCanFailTestCase(TestCase):
    """The anti-vacuity guard: a candidate is promotable ONLY if its grader can FAIL."""

    def test_grounded_candidate_grader_is_proven_able_to_fail(self) -> None:
        result = promote.guard_can_fail(_GROUNDED_CANDIDATE)
        assert result.can_fail is True
        assert "proven to FAIL" in result.reason

    def test_vacuous_candidate_is_rejected_grader_cannot_fail(self) -> None:
        # The guard-disabled probe: the grader PASSES the known-bad transcript, so
        # the matcher guards nothing. The guard MUST reject it (RED).
        result = promote.guard_can_fail(_GROUNDED_CANDIDATE, spec_builder=_vacuous_spec_builder)
        assert result.can_fail is False
        assert "vacuous" in result.reason.lower()

    def test_malformed_candidate_is_rejected_not_crash(self) -> None:
        # A candidate that fails to build a valid spec is a reject, never a traceback.
        def _broken_builder(_c: dict[str, object]) -> EvalSpec:
            msg = "no scenario could be built"
            raise ValueError(msg)

        result = promote.guard_can_fail(_GROUNDED_CANDIDATE, spec_builder=_broken_builder)
        assert result.can_fail is False

    def test_tautology_candidate_is_rejected_grader_fails_even_compliant(self) -> None:
        # A spec whose matcher even the compliant _pass transcript cannot satisfy
        # is a tautology that pins nothing useful — the guard must reject it.
        def _tautology_builder(candidate: dict[str, object]) -> EvalSpec:
            entry = {
                "name": str(candidate["scenario_name"]),
                "scenario": "tautology probe — matcher no trajectory satisfies",
                "agent_path": "skills/rules/SKILL.md",
                "lane": "under_load",
                "model": "haiku",
                "max_turns": 3,
                "tools": ["Bash"],
                "prompt": "x",
                # No transcript ever issues this tool call, so BOTH _fail and _pass
                # grade FAIL → the guard catches the tautology before the teeth check
                # would otherwise pass, and rejects.
                "expect": [{"tool_call": "WebFetch", "args.url": '~ "never-emitted"'}],
            }
            return _parse_spec(entry, SCENARIOS_DIR / "tautology_probe.yaml", None)

        result = promote.guard_can_fail(_GROUNDED_CANDIDATE, spec_builder=_tautology_builder)
        assert result.can_fail is False


class PromoteCandidateCreatesRunnableScenarioTestCase(TestCase):
    """A promoted candidate becomes a discoverable, anti-vacuous, runnable scenario."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scenarios = self.tmp / "scenarios"
        self.fixtures = self.tmp / "fixtures"

    def _promote(self, candidate: dict[str, object]) -> promote.PromotionOutcome:
        return promote.promote_candidate(
            candidate, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures, live_gate=_PASS_GATE
        )

    def test_promotion_writes_a_loadable_scenario_and_two_fixtures(self) -> None:
        outcome = self._promote(_GROUNDED_CANDIDATE)
        assert outcome.promoted is True
        assert outcome.scenario_path is not None
        assert outcome.scenario_path.is_file()
        assert outcome.fail_fixture is not None
        assert outcome.fail_fixture.is_file()
        assert outcome.pass_fixture is not None
        assert outcome.pass_fixture.is_file()
        # The YAML loads back through the real loader (a runnable scenario).
        specs = load_eval_yaml(outcome.scenario_path)
        assert [s.name for s in specs] == ["derived_delegate_under_load"]
        assert specs[0].lane == "under_load"

    def test_promoted_scenario_is_anti_vacuous_fail_red_pass_green(self) -> None:
        outcome = self._promote(_GROUNDED_CANDIDATE)
        spec = load_eval_yaml(outcome.scenario_path)[0]
        fail_run = promote._run_from_transcript(spec.name, outcome.fail_fixture.read_text(encoding="utf-8"))
        pass_run = promote._run_from_transcript(spec.name, outcome.pass_fixture.read_text(encoding="utf-8"))
        # The canonical anti-vacuity contract: _fail grades FAIL, _pass grades PASS.
        assert evaluate(spec, fail_run).verdict == "fail"
        assert evaluate(spec, pass_run).verdict == "pass"

    def test_reject_writes_nothing(self) -> None:
        outcome = self._promote(_GROUNDED_CANDIDATE)
        # Sanity: this candidate IS promotable; to test the reject-writes-nothing
        # path use a candidate with no scenario_name (a guaranteed reject).
        assert outcome.promoted is True  # baseline
        empty = promote.promote_candidate(
            {"drift_rule": "x"}, scenarios_dir=self.tmp / "x", fixtures_dir=self.tmp / "y", live_gate=_PASS_GATE
        )
        assert empty.promoted is False
        assert not (self.tmp / "x").exists()
        assert not (self.tmp / "y").exists()

    def test_dry_run_passes_guard_but_writes_nothing(self) -> None:
        outcome = promote.promote_candidate(
            _GROUNDED_CANDIDATE,
            scenarios_dir=self.scenarios,
            fixtures_dir=self.fixtures,
            dry_run=True,
            live_gate=_PASS_GATE,
        )
        assert outcome.promoted is True
        assert not self.scenarios.exists()
        assert not self.fixtures.exists()

    def test_re_promotion_is_idempotent_no_duplicate_scenario_names(self) -> None:
        self._promote(_GROUNDED_CANDIDATE)
        self._promote(_GROUNDED_CANDIDATE)
        names = promote.loaded_scenario_names(self.scenarios / "promoted_drift.yaml")
        assert names.count("derived_delegate_under_load") == 1

    def test_guard_reject_writes_no_files(self) -> None:
        # When the (non-bypassable) guard rejects, promote_candidate writes nothing
        # and surfaces the guard's reason — the unproven candidate never lands.
        reject = promote.GuardResult(can_fail=False, reason="matchers are vacuous")
        with patch("teatree.loops.dream.promote.guard_can_fail", return_value=reject):
            outcome = self._promote(_GROUNDED_CANDIDATE)
        assert outcome.promoted is False
        assert "anti-vacuity" in outcome.reason.lower()
        assert not self.scenarios.exists()
        assert not self.fixtures.exists()

    def test_loaded_scenario_names_missing_file_is_empty(self) -> None:
        assert promote.loaded_scenario_names(self.tmp / "absent.yaml") == []


class PromotionGatedOnLiveModelPassTestCase(TestCase):
    """A scenario lands ONLY when it PASSES a live-model pass@k — never on the guard alone.

    The anti-vacuity guard proves the grader has teeth against SYNTHETIC fixtures;
    it never checks the scenario actually passes against a real model. Two of the
    three previously auto-promoted scenarios failed a live pass@3 because the
    one-size templated grader did not fit the rule. So promotion is now gated on a
    live pass@k: without a live check NOTHING auto-lands (the safety property), a
    live-FAIL withholds, and only a live-PASS writes the scenario + fixtures. The
    validator is injected and FAKE here — no test ever runs a real metered model.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scenarios = self.tmp / "scenarios"
        self.fixtures = self.tmp / "fixtures"

    def test_live_pass_writes_scenario_and_fixtures(self) -> None:
        outcome = promote.promote_candidate(
            _GROUNDED_CANDIDATE,
            scenarios_dir=self.scenarios,
            fixtures_dir=self.fixtures,
            live_gate=_PASS_GATE,
        )
        assert outcome.promoted is True
        assert outcome.scenario_path is not None
        assert outcome.scenario_path.is_file()
        assert outcome.fail_fixture is not None
        assert outcome.fail_fixture.is_file()
        assert outcome.pass_fixture is not None
        assert outcome.pass_fixture.is_file()

    def test_live_fail_withholds_and_writes_nothing(self) -> None:
        outcome = promote.promote_candidate(
            _GROUNDED_CANDIDATE,
            scenarios_dir=self.scenarios,
            fixtures_dir=self.fixtures,
            live_gate=_FAIL_GATE,
        )
        assert outcome.promoted is False
        assert "withheld" in outcome.reason.lower()
        assert "live-model" in outcome.reason.lower()
        assert not self.scenarios.exists()
        assert not self.fixtures.exists()

    def test_no_validator_withholds_the_safety_property(self) -> None:
        # The KEY safety property: a deterministic-only promote (no metered check —
        # nightly tick, or no claude/auth) NEVER auto-lands a scenario in the gating
        # suite. The candidate clears scrub + anti-vacuity but is still withheld.
        outcome = promote.promote_candidate(
            _GROUNDED_CANDIDATE,
            scenarios_dir=self.scenarios,
            fixtures_dir=self.fixtures,
            live_gate=None,
        )
        assert outcome.promoted is False
        assert "withheld" in outcome.reason.lower()
        assert "not run" in outcome.reason.lower()
        assert not self.scenarios.exists()
        assert not self.fixtures.exists()

    def test_default_validator_is_none_so_promotion_is_withheld(self) -> None:
        # Defaulting the param to None means existing callers that pass no validator
        # withhold by default — nothing lands without an explicit metered check.
        outcome = promote.promote_candidate(
            _GROUNDED_CANDIDATE, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures
        )
        assert outcome.promoted is False
        assert "not run" in outcome.reason.lower()

    def test_validator_receives_the_candidate_spec_and_pass_at_k_params(self) -> None:
        # The validator is handed the would-be scenario's OWN spec and the pass@k
        # knobs, so it meters exactly the scenario that would land.
        seen: dict[str, object] = {}

        def _capture(spec: EvalSpec, *, trials: int, require: str) -> bool:
            seen["name"] = spec.name
            seen["trials"] = trials
            seen["require"] = require
            return True

        promote.promote_candidate(
            _GROUNDED_CANDIDATE,
            scenarios_dir=self.scenarios,
            fixtures_dir=self.fixtures,
            live_gate=promote.LiveGate(validator=_capture, trials=5, require="all"),
        )
        assert seen["name"] == "derived_delegate_under_load"
        assert seen["trials"] == 5
        assert seen["require"] == "all"

    def test_live_fail_is_terminal_rejected_in_the_queue(self) -> None:
        # A live-FAIL is a verdict the candidate's grader does not fit the model —
        # terminal-rejected, not retried indefinitely.
        queue = self.tmp / "proposals.jsonl"
        queue.write_text(json.dumps(_GROUNDED_CANDIDATE) + "\n", encoding="utf-8")
        promote.promote_proposals_file(
            queue, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures, live_gate=_FAIL_GATE
        )
        row = json.loads(queue.read_text(encoding="utf-8").splitlines()[0])
        assert row["status"] == "rejected"

    def test_no_validation_is_retryable_in_the_queue(self) -> None:
        # A withheld-for-no-validation candidate stays RETRYABLE: not terminal, so a
        # later validated run can still land it. Its queue status is NOT 'promoted'.
        queue = self.tmp / "proposals.jsonl"
        queue.write_text(json.dumps(_GROUNDED_CANDIDATE) + "\n", encoding="utf-8")
        promote.promote_proposals_file(queue, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures, live_gate=None)
        row = json.loads(queue.read_text(encoding="utf-8").splitlines()[0])
        assert row["status"] != promote._PROMOTED_STATUS
        # A second pass WITH a passing validator now lands it (it was retryable).
        second = promote.promote_proposals_file(
            queue, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures, live_gate=_PASS_GATE
        )
        assert [o.promoted for o in second] == [True]


class PromotedScenarioIsPublishSafeTestCase(TestCase):
    """The promoted YAML + fixtures are publish-safe BY CONSTRUCTION (no leak reaches the gate).

    ``context_preamble`` and ``seed_citation`` are distilled from the operator's
    private memory / session transcripts; copied verbatim they leak customer forge
    refs / names into the PUBLIC repo (the ``banned-terms`` pre-commit hook caught
    one late). The writer must neutralise bare forge refs first, then withhold the
    scenario only if a banned term survives — so a scrubbed scenario still has
    grader teeth, and a banned NAME is never emitted.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scenarios = self.tmp / "scenarios"
        self.fixtures = self.tmp / "fixtures"

    def _written_text(self, outcome: promote.PromotionOutcome) -> str:
        return "\n".join(
            p.read_text(encoding="utf-8") for p in (outcome.scenario_path, outcome.fail_fixture, outcome.pass_fixture)
        )

    def test_bare_forge_ref_is_neutralised_and_grader_keeps_teeth(self) -> None:
        # A candidate whose drift_rule / seed_citation carry a bare forge ref with a
        # customer org. Neutralisation defangs the ref; nothing banned survives, so
        # the scenario IS promoted — and the written YAML + both fixtures carry NO
        # bare reference, while the grader still FAILs the drift and PASSes compliance.
        leaky = {
            **_GROUNDED_CANDIDATE,
            "scenario_name": "leaky_forge_ref",
            "drift_rule": "the agent edited code in the foreground, see !4521 instead of dispatching",
            "seed_citation": "edited src/teatree/core/session.py in the main agent, cited #9912",
        }
        outcome = promote.promote_candidate(
            leaky, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures, live_gate=_PASS_GATE
        )
        assert outcome.promoted is True

        written = self._written_text(outcome)
        assert find_bare_references(written) == []
        # The raw bare sigils must NOT survive anywhere in the committed artefacts.
        assert "!4521" not in written
        assert "#9912" not in written

        # The scrub touched only human-readable text — the grader still has teeth.
        spec = load_eval_yaml(outcome.scenario_path)[0]
        fail_run = promote._run_from_transcript(spec.name, outcome.fail_fixture.read_text(encoding="utf-8"))
        pass_run = promote._run_from_transcript(spec.name, outcome.pass_fixture.read_text(encoding="utf-8"))
        assert evaluate(spec, fail_run).verdict == "fail"
        assert evaluate(spec, pass_run).verdict == "pass"

    def test_banned_name_that_neutralisation_cannot_remove_is_withheld(self) -> None:
        # A candidate whose preamble carries a customer NAME (not inside a forge ref,
        # no safe auto-replacement): neutralisation leaves it, the re-scan still flags
        # it, so the scenario is WITHHELD — promoted=False, no files written, never
        # emitted into the public repo.
        named = {**_GROUNDED_CANDIDATE, "scenario_name": "leaky_customer_name"}
        with patch(
            "teatree.loops.dream.promote.banned_terms_scanner.scan_text",
            return_value="customer-name",
        ):
            outcome = promote.promote_candidate(
                named, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures, live_gate=_PASS_GATE
            )

        assert outcome.promoted is False
        assert "withheld" in outcome.reason.lower()
        assert "customer-name" in outcome.reason
        assert not self.scenarios.exists()
        assert not self.fixtures.exists()


class PromoteProposalsFileTestCase(TestCase):
    """Promoting the whole candidate review-queue JSONL through the guarded path."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scenarios = self.tmp / "scenarios"
        self.fixtures = self.tmp / "fixtures"
        self.queue = self.tmp / "proposals.jsonl"

    def test_promotes_each_row_and_skips_malformed(self) -> None:
        rows = [
            json.dumps(_GROUNDED_CANDIDATE),
            "",  # blank line — skipped, not an error
            "{not valid json",
            json.dumps({"drift_rule": "no name -> reject"}),
            json.dumps(["not", "an", "object"]),  # JSON array row -> reject
        ]
        self.queue.write_text("\n".join(rows) + "\n", encoding="utf-8")
        outcomes = promote.promote_proposals_file(
            self.queue, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures, live_gate=_PASS_GATE
        )
        promoted = [o for o in outcomes if o.promoted]
        assert len(promoted) == 1
        assert promoted[0].scenario_name == "derived_delegate_under_load"
        # The malformed JSON + no-name + non-object rows are rejects, not crashes
        # (the blank line is skipped entirely, not even an outcome row).
        assert sum(1 for o in outcomes if not o.promoted) == 3

    def test_missing_queue_is_empty_list(self) -> None:
        assert promote.promote_proposals_file(self.tmp / "absent.jsonl") == []

    def _queue_rows(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.queue.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_writes_status_back_to_the_queue(self) -> None:
        rows = [
            json.dumps(_GROUNDED_CANDIDATE),
            json.dumps({**_GROUNDED_CANDIDATE, "scenario_name": "no_drift_rule_reject", "drift_rule": ""}),
        ]
        # The second row's matcher is satisfiable by the bad transcript -> guard rejects.
        rejecting = {**_GROUNDED_CANDIDATE, "scenario_name": "rejected_candidate"}
        del rejecting["drift_rule"]
        rows[1] = json.dumps(rejecting)
        self.queue.write_text("\n".join(rows) + "\n", encoding="utf-8")

        with patch.object(
            promote,
            "promote_candidate",
            side_effect=[
                promote.PromotionOutcome(scenario_name="derived_delegate_under_load", promoted=True, reason="ok"),
                promote.PromotionOutcome(scenario_name="rejected_candidate", promoted=False, reason="REJECTED"),
            ],
        ):
            promote.promote_proposals_file(self.queue, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures)

        written = self._queue_rows()
        by_name = {r["scenario_name"]: r for r in written}
        assert by_name["derived_delegate_under_load"]["status"] == "promoted"
        assert by_name["rejected_candidate"]["status"] == "rejected"
        # The original fields are preserved alongside the new status.
        assert by_name["derived_delegate_under_load"]["lane"] == "under_load"
        assert "promotion_reason" in by_name["derived_delegate_under_load"]

    def test_second_call_skips_already_promoted_rows(self) -> None:
        self.queue.write_text(json.dumps(_GROUNDED_CANDIDATE) + "\n", encoding="utf-8")
        first = promote.promote_proposals_file(
            self.queue, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures, live_gate=_PASS_GATE
        )
        assert [o.promoted for o in first] == [True]
        scenario_file = self.scenarios / "promoted_drift.yaml"
        names_after_first = list(promote.loaded_scenario_names(scenario_file))

        # A second run must SKIP the already-promoted row: no re-promotion, no duplicate.
        with patch.object(promote, "promote_candidate") as promote_fn:
            second = promote.promote_proposals_file(
                self.queue, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures
            )
            promote_fn.assert_not_called()
        assert [o.promoted for o in second] == [True]
        assert second[0].reason.startswith("already promoted")
        # The scenario file was not duplicated.
        assert list(promote.loaded_scenario_names(scenario_file)) == names_after_first

    def test_dry_run_leaves_the_queue_byte_identical(self) -> None:
        rows = [json.dumps(_GROUNDED_CANDIDATE), json.dumps({**_GROUNDED_CANDIDATE, "scenario_name": "second"})]
        original = "\n".join(rows) + "\n"
        self.queue.write_text(original, encoding="utf-8")
        before = self.queue.read_bytes()
        promote.promote_proposals_file(
            self.queue, scenarios_dir=self.scenarios, fixtures_dir=self.fixtures, dry_run=True
        )
        assert self.queue.read_bytes() == before


class ExtractedModulesTestCase(TestCase):
    """The live-gate / outcome / transcript concerns are split out yet re-exported from promote."""

    def test_promote_reexports_the_extracted_symbols_by_identity(self) -> None:
        assert promote.LiveGate is live_gate.LiveGate
        assert promote.LiveValidator is LiveValidator
        assert promote.build_live_validator is build_live_validator
        assert promote._fail_transcript is fail_transcript
        assert promote._pass_transcript is pass_transcript
        assert promote._run_from_transcript is run_from_transcript

    def test_build_live_validator_returns_a_validator_and_runs_no_model_here(self) -> None:
        # The real validator is METERED, so we only assert it builds a callable with
        # the LiveValidator shape — never invoke it (no model call in the suite).
        validator: LiveValidator = build_live_validator()
        assert callable(validator)

    def test_validator_builds_the_metered_runner_and_aggregates_pass_at_k(self) -> None:
        # Drive the returned ``_validate`` body with the metered runner construction
        # and pass@k aggregation faked, so the validator's real body is covered
        # without a model call: it builds an ApiInProcessRunner(require_executed=True)
        # and returns the pass@k verdict's ``.ok``. The validator's lazy imports bind
        # at build time, so build it INSIDE the patch context.
        from unittest.mock import MagicMock  # noqa: PLC0415

        spec = promote._candidate_spec(dict(_GROUNDED_CANDIDATE))
        with (
            patch("teatree.eval.api_runner.ApiInProcessRunner") as runner_cls,
            patch("teatree.eval.pass_at_k.run_pass_at_k", return_value=MagicMock(ok=True)) as run_k,
        ):
            validator: LiveValidator = build_live_validator()
            assert validator(spec, trials=3, require="any") is True
        runner_cls.assert_called_once_with(require_executed=True)
        assert run_k.call_args.kwargs == {"k": 3, "require": "any"}

    def test_synthetic_transcripts_round_trip_through_run_from_transcript(self) -> None:
        fail_run = run_from_transcript("probe", fail_transcript("probe", "the cited rule"))
        pass_run = run_from_transcript("probe", pass_transcript("probe", "the cited rule"))
        assert any(call.name == "Edit" for call in fail_run.tool_calls)
        assert any(call.name == "Task" for call in pass_run.tool_calls)

    def test_live_gate_default_validator_none_withholds_retryable(self) -> None:
        spec = promote._candidate_spec(dict(_GROUNDED_CANDIDATE))
        verdict = live_gate.LiveGate().verdict(spec)
        assert verdict is not None
        assert verdict.retryable is True
        assert not verdict.promoted

"""Tests for the completion-claim gate detector (issue #2665).

The agent emits a completeness assertion — "done", "no blockers anywhere",
"ready to merge" — from the artifacts it produced reviewing clean, NOT from
every spec-defined deliverable verified on the actual merge target. The
representative failure: "no blockers anywhere" on a multi-deliverable ticket
while the crucial deliverable was on the wrong surface and its fix stranded off
the merge target.

The detector ``completion_claim_scanner`` and the BLOCKING Stop handler
``handle_completion_claim_gate`` promote the prose verification-before-completion
rule to a deterministic gate. Unlike the WARN-only closure-reverify sibling,
this one BLOCKS — so the load-bearing tests are the NO-FIRE cases: a false
block would wedge a legitimate single-deliverable "done".

Two layers, both integration-style: the pure detector exercised directly, and
the real ``hook_router`` Stop handler exercised through a real transcript JSONL
written under ``tmp_path`` (only stdin/stdout cross the boundary).
"""

from teatree.hooks import completion_claim_scanner as scanner

# A complete, on-target deliverable->evidence map for a multi-deliverable ticket:
# every deliverable carries on-target evidence, the spec was read, and the
# crucial deliverable is verified on its correct surface. This must NOT fire.
_COMPLETE_MAP = (
    "I read the authoritative spec and its comments and enumerated every deliverable.\n"
    "- Backend serializer change: merged to the merge target, verified on main.\n"
    "- Crucial deliverable (the authoring UI): verified on the correct config surface.\n"
    "- Frontend banner: passing E2E, evidence posted.\n"
    "All deliverables are done on the merge target.\n"
)

# The real-incident transcript: "no blockers anywhere" on a multi-deliverable
# ticket where one deliverable is on the wrong surface and stranded off target —
# BUT phrased as a confident claim, NOT an honest refusal. This must FIRE.
_STRANDED_CLAIM = (
    "Reviewed all the open MRs — no blockers anywhere.\n"
    "- Backend change: MR opened.\n"
    "- Authoring UI: MR opened.\n"
    "- Frontend banner: MR opened.\n"
    "Everything is here and ready to merge.\n"
)


class TestFiresOnMultiDeliverableClaimWithoutMap:
    """A completeness claim on a multi-deliverable ticket with no on-target map."""

    def test_real_incident_stranded_claim_fires(self) -> None:
        verdict = scanner.find_completion_block(_STRANDED_CLAIM)
        assert verdict is not None
        assert verdict.deliverable_count == 3
        assert verdict.missing  # at least one incomplete leg

    def test_artifact_existence_is_not_evidence(self) -> None:
        text = "No blockers anywhere.\n- Deliverable A: PR #10 created.\n- Deliverable B: PR #11 created.\nDone.\n"
        verdict = scanner.find_completion_block(text)
        assert verdict is not None
        assert any("on-target evidence" in reason for reason in verdict.missing)

    def test_unread_spec_is_an_incomplete_leg(self) -> None:
        text = (
            "- Deliverable A: merged to the merge target.\n"
            "- Crucial deliverable: verified on the correct surface.\n"
            "Everything is done and ready to merge.\n"
        )
        verdict = scanner.find_completion_block(text)
        assert verdict is not None
        assert any("authoritative spec" in reason for reason in verdict.missing)

    def test_unverified_crucial_surface_is_an_incomplete_leg(self) -> None:
        text = (
            "I read the authoritative spec and its comments.\n"
            "- Deliverable A: merged to the merge target.\n"
            "- Deliverable B: merged to main.\n"
            "Everything is done and ready to merge.\n"
        )
        verdict = scanner.find_completion_block(text)
        assert verdict is not None
        assert any("crucial deliverable" in reason for reason in verdict.missing)


class TestDoesNotFire:
    """The load-bearing no-fire guards — a false block wedges a real 'done'."""

    def test_complete_on_target_map_passes(self) -> None:
        assert scanner.find_completion_block(_COMPLETE_MAP) is None

    def test_honest_refusal_never_fires(self) -> None:
        text = (
            "Reviewed the MRs.\n"
            "- Backend change: merged to target.\n"
            "- Authoring UI: MR opened.\n"
            "NOT done: the authoring UI is on the wrong surface and its fix is "
            "stranded off the merge target.\n"
        )
        assert scanner.find_completion_block(text) is None

    def test_single_deliverable_claim_never_fires(self) -> None:
        text = "Fixed the typo in the README.\n- Updated the heading.\nDone."
        assert scanner.find_completion_block(text) is None

    def test_no_completeness_claim_never_fires(self) -> None:
        text = (
            "Status so far:\n"
            "- Deliverable A: in progress.\n"
            "- Deliverable B: not started.\n"
            "Still working through the list.\n"
        )
        assert scanner.find_completion_block(text) is None

    def test_plain_status_with_no_enumeration_never_fires(self) -> None:
        assert scanner.find_completion_block("All three tickets are shipped and pipelines are green.") is None

    def test_empty_text_is_none(self) -> None:
        assert scanner.find_completion_block("") is None


# An architecture-RECOMMENDATION turn enumerating options/decision items with an
# incidental completeness phrase ("ready to go", "I'm done laying out the
# options") — NOTHING is claimed done, there is no tracked multi-deliverable
# ticket. The gate counted the option lines as "deliverables" and demanded an
# evidence map (#2665 false positive). It must NOT fire.
_ARCHITECTURE_RECOMMENDATION = (
    "Architecture recommendation for the config layer. Here are the options and "
    "trade-offs to consider:\n"
    "- Option A: DB-only runtime, TOML export only. Trade-off: simpler reads.\n"
    "- Option B: dual-tier DB + TOML. Trade-off: flexible but drift-prone.\n"
    "- Pattern 1: a Django-free sqlite cold reader.\n"
    "- Decision item: where does workspace_dir live?\n"
    "- Decision item: how to migrate existing config?\n"
    "- Recommendation: go with Option A.\n"
    "- Open question: keep loops/presets in one registry?\n"
    "That's my draft proposal — I'm done laying out the options, ready to go when "
    "you decide.\n"
)


class TestRecommendationProseNeverFires:
    """Anti-vacuous pair: the recommendation FP passes; the real claim still fires."""

    def test_architecture_recommendation_does_not_fire(self) -> None:
        # The false positive: options/decisions enumeration with an incidental
        # "ready to go" / "I'm done" must NOT read as a completion claim.
        assert scanner.find_completion_block(_ARCHITECTURE_RECOMMENDATION) is None

    def test_real_multideliverable_claim_still_fires(self) -> None:
        # Proves the recommendation guard did not weaken the gate: the real
        # stranded multi-deliverable done-claim (no recommendation frame, zero
        # option-shaped lines) still blocks.
        verdict = scanner.find_completion_block(_STRANDED_CLAIM)
        assert verdict is not None
        assert verdict.deliverable_count == 3

    def test_recommendation_frame_alone_does_not_exempt_a_work_claim(self) -> None:
        # A real done-claim that merely USES "recommend" but enumerates delivered
        # WORK (not options) is NOT exempted — the line-majority leg fails, so the
        # gate still fires. Guards against the frame regex over-exempting.
        text = (
            "I recommend merging now — no blockers anywhere.\n"
            "- Backend serializer: MR opened.\n"
            "- Authoring UI: MR opened.\n"
            "- Frontend banner: MR opened.\n"
            "Everything is here and ready to merge.\n"
        )
        assert scanner.find_completion_block(text) is not None


# A PURE design-discussion turn (#2665 over-fire): a decision table whose rows are
# locked design choices, with a "we're done / ready to go" sign-off — but NO active
# ticket and NO delivery context (no MR/PR, branch, merge, commit, deliverable,
# ticket, E2E). The gate read the 6 decision rows as "6 deliverables" and the
# locked/done wording as a multi-deliverable completion claim, forcing the agent to
# escape with [skip-completion-gate]. As a no-delivery-context design table it must
# NOT fire.
_DESIGN_DECISION_TABLE = (
    "Locking in the design decisions for the integration factory:\n"
    "- Stack: build directly on a thin Python harness on the Claude Agent SDK (locked)\n"
    "- Runtime: no sandcastle runtime, build native from day one (locked)\n"
    "- Hosting: run the model via Vertex aligned with the GCP infra (locked)\n"
    "- Client libs: adopt FastMCP plus openapi-python-client (locked)\n"
    "- Token budget: per-endpoint metering analysis (locked)\n"
    "- Measurement: 10x baseline vs the manual approach (locked)\n"
    "Everything is locked. We're done here — ready to go when you start the build.\n"
)

# A genuine multi-deliverable false-"done" that uses NO delivery vocabulary at all
# (no MR/PR/merge/branch/commit/ticket/E2E) — just enumerated units of work claimed
# in place with no evidence map, on a loop-driven turn. The reviewer's over-exemption
# finding: the prior delivery-grounding requirement let this slip through silently.
# It is NOT a design-decision table (no locked/decided rows, no design frame), so it
# MUST still fire.
_NO_DELIVERY_VOCAB_FALSE_DONE = (
    "Done - everything is in place:\n- validation logic added\n- error handling added\n- UI button wired up\n"
)

# A done-claim resting on "tests written" with no delivery vocab — also previously
# slipped through the grounding requirement. Must still fire.
_TESTS_WRITTEN_FALSE_DONE = "Everything is done:\n- core logic implemented\n- tests written\n- edge cases handled\n"


class TestDesignDecisionTableNeverFires:
    """Anti-vacuous pair: the no-delivery-context design FP passes; a grounded claim fires."""

    def test_design_decision_table_does_not_fire(self) -> None:
        # The over-fire: a decision table + "we're done / ready to go" sign-off with
        # NO delivery context must NOT read as a completion claim. Reverting the
        # design-table suppression makes this fire — the RED-on-revert anchor.
        assert scanner.find_completion_block(_DESIGN_DECISION_TABLE) is None

    def test_delivery_grounded_multideliverable_claim_still_fires(self) -> None:
        # Proves the design-table suppression did not neuter the gate: the same
        # enumerated-and-claimed shape, once it cites real delivery artifacts (MRs,
        # the merge target), still blocks.
        verdict = scanner.find_completion_block(_STRANDED_CLAIM)
        assert verdict is not None
        assert verdict.deliverable_count == 3


class TestMultiDeliverableFalseDoneWithoutDeliveryVocabStillFires:
    """Reviewer regression: a no-evidence multi-deliverable done-claim fires with no delivery vocab."""

    def test_no_delivery_vocab_false_done_fires(self) -> None:
        # The reviewer's over-exemption finding: a multi-deliverable false-"done" that
        # omits all delivery words must STILL block. Previously returned None.
        verdict = scanner.find_completion_block(_NO_DELIVERY_VOCAB_FALSE_DONE)
        assert verdict is not None
        assert verdict.deliverable_count == 3
        assert verdict.missing

    def test_tests_written_grounded_false_done_fires(self) -> None:
        # A done-claim resting on "tests written" with no delivery vocab must STILL
        # block. Previously returned None under the grounding requirement.
        verdict = scanner.find_completion_block(_TESTS_WRITTEN_FALSE_DONE)
        assert verdict is not None
        assert verdict.deliverable_count == 3
        assert verdict.missing


# The #2665 over-fire: a real ship report binds each deliverable to a 40-hex merge
# commit SHA + a MERGED PR/MR state + origin/main HEAD + fast-forward — the
# STRONGEST proof it landed on the merge target. The on-target recognizer enumerated
# only English "landed on target" phrases, so every machine-grade row fell into
# lines_without_evidence and the gate over-fired "N of N deliverables lack on-target
# evidence". (Enumerated as list lines, not a `| … |` table — a pure markdown table
# returns None for a different reason: its rows do not match _DELIVERABLE_LINE_RE.)
# The spec-read and crucial-surface legs are confirmed too, so the ONLY leg the
# recognizer extension clears is on-target — making this the RED-on-revert anchor.
_MERGE_SHA_EVIDENCE_MAP = (
    "I read the authoritative spec and its comments and enumerated every deliverable.\n"
    "Both deliverables are merged and live on main — done.\n"
    "- US-03 EURIBOR endpoint: MR !41 MERGED, merge commit "
    "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0, origin/main HEAD = "
    "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0, fast-forwarded.\n"
    "- US-05 product-items endpoint: MR !42 MERGED, merge commit "
    "0f1e2d3c4b5a69788796a5b4c3d2e1f0a9b8c7d6, origin/main HEAD = "
    "0f1e2d3c4b5a69788796a5b4c3d2e1f0a9b8c7d6, fast-forwarded.\n"
    "The crucial deliverable (US-03) is verified on its correct surface.\n"
    "Both US-03 and US-05 are fixed and live on main.\n"
)

# The sibling over-exemption guard (#2842 must not re-open): a multi-deliverable
# "done" whose ONLY evidence is "an MR/PR exists" — a PR url/number with NO merge
# commit SHA and NO MERGED state. The spec-read and crucial-surface legs are
# confirmed, so the on-target leg is the only thing that can keep it firing — and it
# MUST, because neither row carries a merge SHA or a MERGED token.
_MR_EXISTS_ONLY_FALSE_DONE = (
    "I read the authoritative spec and its comments.\n"
    "Both deliverables are done and ready to merge.\n"
    "- US-03 EURIBOR endpoint: MR !41 opened, ready for review.\n"
    "- US-05 product-items endpoint: PR #42 created, branch pushed.\n"
    "The crucial deliverable (US-03) is verified on its correct surface.\n"
)


class TestMergeShaEvidenceClears:
    """Anti-vacuous pair: the merge-SHA over-fire clears; the MR-exists-only claim fires."""

    def test_merge_sha_evidence_map_does_not_fire(self) -> None:
        # The over-fire: each row bound to a merge commit SHA + MERGED + origin/main
        # HEAD + fast-forward is a COMPLETE on-target map. Reverting the recognizer
        # alternations makes this fire — the RED-on-revert anchor.
        assert scanner.find_completion_block(_MERGE_SHA_EVIDENCE_MAP) is None

    def test_mr_exists_only_false_done_still_fires(self) -> None:
        # The #2842 sibling over-exemption must NOT re-open: "MR opened" / "PR created"
        # with no merge SHA and no MERGED state is not on-target evidence, so the
        # multi-deliverable claim STILL blocks. Stays GREEN when the recognizer is
        # reverted — proving the positive test isolates the recognizer, not the legs.
        verdict = scanner.find_completion_block(_MR_EXISTS_ONLY_FALSE_DONE)
        assert verdict is not None
        assert verdict.deliverable_count == 2
        assert any("on-target evidence" in reason for reason in verdict.missing)


class TestFormatBlockMessage:
    def test_message_names_the_incomplete_legs(self) -> None:
        verdict = scanner.find_completion_block(_STRANDED_CLAIM)
        assert verdict is not None
        message = scanner.format_block_message(verdict)
        assert "COMPLETION-CLAIM GATE (#2665)" in message
        assert "NOT done" in message
        for reason in verdict.missing:
            assert reason in message

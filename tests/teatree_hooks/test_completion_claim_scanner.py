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


class TestFormatBlockMessage:
    def test_message_names_the_incomplete_legs(self) -> None:
        verdict = scanner.find_completion_block(_STRANDED_CLAIM)
        assert verdict is not None
        message = scanner.format_block_message(verdict)
        assert "COMPLETION-CLAIM GATE (#2665)" in message
        assert "NOT done" in message
        for reason in verdict.missing:
            assert reason in message

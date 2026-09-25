"""One failure-kind → recovery-strategy table, read by both axes (#4505).

The classification and the recovery decision used to be two hand-maintained lists that never
consulted each other: ``_ENVIRONMENTAL`` (keyed on the kind) and ``_TRANSIENT_MARKERS`` (keyed on
error text). ``harness_crash`` was environmental by classification and absent from the requeue
predicate, which dropped eleven tasks in a day. These tests pin the replacement: every kind has an
explicit strategy, the retry set is a subset of the environmental one, and the strategy a real error
string resolves to is the one the deleted text predicate produced.
"""

import pytest

from teatree.core.gates.closed_issue_dispatch_gate import ISSUE_CLOSED_PREFIX
from teatree.core.gates.plan_dispatch_gate import PLAN_MISSING_PREFIX
from teatree.core.modelkit.task_failure_taxonomy import (
    COMPACTION_BLOCKED_MARKER,
    CONTEXT_EXHAUSTED_MARKER,
    HEAD_SUPERSEDED_PREFIX,
    RECOVERY,
    RESULT_ERROR_MARKER,
    REVIEW_UNRECORDABLE_PREFIX,
    SUPERSEDED_PREFIX,
    FailureKind,
    RecoveryStrategy,
    classify_failure,
    exhausted_the_conversation,
    is_causeless,
    is_environmental,
    recovery_strategy,
    stall_fingerprints,
    stall_kinds,
)
from teatree.llm.anthropic_limits import LimitCause, LimitMatch

#: The environmental set as it stood before the table absorbed it. Spelled out literally rather than
#: derived, so the refactor cannot silently move the operator's diagnostic axis — which is also
#: ``stall_kinds``' filter.
_ENVIRONMENTAL_BEFORE = {
    FailureKind.LEASE_LOST,
    FailureKind.LEASE_EXPIRED,
    FailureKind.USAGE_LIMIT_PARKED,
    FailureKind.CREDENTIAL_EXHAUSTED,
    FailureKind.HARNESS_CRASH,
    FailureKind.OUTAGE,
    FailureKind.RESULT_ERROR,
    FailureKind.PROVISION_FAILED,
    FailureKind.LANDING_UNVERIFIED,
}

#: Kinds added to the axis SINCE, one deliberate line each. The guard below still pins every
#: pre-existing kind exactly, so a silent move of the original set stays red; only a named
#: addition passes.
_ENVIRONMENTAL_ADDED = {
    # A moved PR head is not a fault in the work — souliane/teatree#4737.
    FailureKind.HEAD_SUPERSEDED,
}


class TestEveryKindHasAStrategy:
    def test_the_table_is_total_over_failure_kind(self) -> None:
        """Both directions: a new kind with no row is red, and so is a row for a deleted kind."""
        assert set(RECOVERY) == set(FailureKind)

    @pytest.mark.parametrize("kind", list(FailureKind))
    def test_each_kind_resolves_to_a_named_strategy(self, kind: FailureKind) -> None:
        assert recovery_strategy(kind) in set(RecoveryStrategy)

    def test_an_unknown_kind_never_auto_reopens(self) -> None:
        """A kind this build does not know is HALTed, never retried — the safe default."""
        assert recovery_strategy("some_kind_from_a_newer_build") is RecoveryStrategy.HALT
        assert recovery_strategy("") is RecoveryStrategy.HALT


class TestTheOneWayInvariant:
    """Everything the sweep retries is environmental; never the reverse (BLUEPRINT §4)."""

    def test_every_retried_kind_is_environmental(self) -> None:
        retried = {kind for kind, recovery in RECOVERY.items() if recovery.strategy is RecoveryStrategy.RETRY}
        assert retried <= {kind for kind, recovery in RECOVERY.items() if recovery.environmental}

    def test_the_reverse_does_not_hold(self) -> None:
        """LEASE_LOST is environmental yet must not be reopened — a live successor holds the work."""
        assert is_environmental(FailureKind.LEASE_LOST)
        assert recovery_strategy(FailureKind.LEASE_LOST) is RecoveryStrategy.HALT


class TestTheEnvironmentalAxisIsUnmoved:
    def test_the_column_matches_the_pre_table_set(self) -> None:
        environmental = {kind for kind, recovery in RECOVERY.items() if recovery.environmental}
        assert environmental == _ENVIRONMENTAL_BEFORE | _ENVIRONMENTAL_ADDED

    @pytest.mark.parametrize("kind", sorted(_ENVIRONMENTAL_BEFORE))
    def test_is_environmental_still_answers_for_each(self, kind: str) -> None:
        assert is_environmental(kind)

    def test_an_unclassified_failure_is_not_environmental(self) -> None:
        assert not is_environmental(FailureKind.UNCLASSIFIED)


class TestStrategyOfARealErrorString:
    """The differential: the strategy a real error resolves to matches the deleted text predicate.

    Both corpora are the ones ``tests/test_failure_signatures.py`` used to assert ``is_transient_failure``
    against, so a drift between the old predicate and the new table shows up here rather than in
    production.
    """

    @pytest.mark.parametrize(
        "error",
        [
            "outage_death: connection refused",
            "provision_failed: db import returned 0 rows",
            "result_error: no terminal ResultMessage — the run ended without completing",
            "result_error: subtype=error_during_execution — api_error_status=529",
            "landing_unverified: no new commit on the branch",
            "Unable to connect to API",
            "API Error: connection reset by peer",
            "RESULT_ERROR: NO TERMINAL RESULTMESSAGE",
        ],
    )
    def test_an_interrupted_run_is_retried(self, error: str) -> None:
        assert recovery_strategy(classify_failure(error)) is RecoveryStrategy.RETRY

    @pytest.mark.parametrize(
        "error",
        [
            "AssertionError: expected 3 got 4",
            "test_widget_renders FAILED: ValueError",
            "stuck_loop: turns ceiling exceeded",
            "Added API Error handling and retries",
            "",
            "cancelled: operator stopped the run",
            "lease_expired: lease held by worker-3 expired without a heartbeat and was reaped",
            "missing required evidence for phase 'coding': result must include one of [files_modified]",
            "Agent result contains unexpected keys: bogus",
            "review verdict recording refused: reviewer identity is a maker role",
        ],
    )
    def test_a_deterministic_failure_is_never_retried(self, error: str) -> None:
        assert recovery_strategy(classify_failure(error)) is not RecoveryStrategy.RETRY

    @pytest.mark.parametrize(
        "error",
        [
            "Traceback (most recent call last):\n  File ...\nException: boom",
            "ProcessError: the agent process exited with code 1",
        ],
    )
    def test_a_harness_crash_is_retried(self, error: str) -> None:
        """The #4439 evidence: environmental by classification, dropped by the old text predicate.

        This is the corpus ``tests/test_failure_signatures.py`` asserted ``is_transient_failure``
        against before #4505 deleted that predicate. It is carried here, end to end from the error
        string, because the strategy — not the phrase table — is now what decides a requeue, and
        it covers BOTH markers: the earlier version exercised only the raw traceback, so a
        regression in the ``processerror`` half would not have been caught.
        """
        assert classify_failure(error) == FailureKind.HARNESS_CRASH
        assert recovery_strategy(classify_failure(error)) is RecoveryStrategy.RETRY

    def test_a_control_request_timeout_is_not_retried(self) -> None:
        """The SDK's control timeout arrives AS a traceback but is not the crash it dresses as.

        It sat in the corpus above, asserted ``HARNESS_CRASH`` and therefore RETRY, because that
        corpus predates the kind having a name. Retrying walks straight back into the same
        session-start deadline — 53 of 60 tasks on the deployed box died that way and read as
        weather — so the named kind HALTs, and the raw-traceback and ``processerror`` halves of
        the crash corpus above keep their retry.
        """
        error = "Traceback (most recent call last):\nException: Control request timeout: initialize"

        assert classify_failure(error) == FailureKind.HARNESS_CONTROL_TIMEOUT
        assert recovery_strategy(classify_failure(error)) is RecoveryStrategy.HALT

    def test_an_api_error_with_a_connection_phrase_is_an_outage(self) -> None:
        """Classification reads the co-occurrence rule, so the router does not lose this outage class."""
        assert classify_failure("API Error: connection reset by peer") == FailureKind.OUTAGE

    def test_an_api_error_in_ordinary_prose_is_not_an_outage(self) -> None:
        assert classify_failure("Added API Error handling and retries") == FailureKind.UNCLASSIFIED


class TestCorrectableFailures:
    """Every kind whose failure a bounded correction can address is named CORRECTIVE_RETRY.

    The set must cover every error the sweep's corrective handlers accept, or the refactor silently
    removes a retry those failures earn today.
    """

    @pytest.mark.parametrize(
        "error",
        [
            "no_result_envelope: agent produced no JSON result envelope; refusing to record success",
            "missing required evidence for phase 'coding': result must include one of [files_modified]",
            "Agent result contains unexpected keys: bogus",
            "Agent result is not valid JSON",
            "Agent result must be a JSON object",
            "agent_harness_provider='anthropic' is not valid under agent_harness='pydantic_ai'",
        ],
    )
    def test_a_correctable_failure_is_named_corrective_retry(self, error: str) -> None:
        assert recovery_strategy(classify_failure(error)) is RecoveryStrategy.CORRECTIVE_RETRY

    def test_a_withheld_verdict_shares_the_kind_but_not_the_correction(self) -> None:
        """The kind says a correction MAY apply; the sweep's own predicate decides whether it does.

        A verdict a gate withheld is a judgement to surface, so ``_corrective_note`` declines it and
        the router escalates — pinned end-to-end in ``tests/teatree_loop/test_transient_requeue.py``.
        """
        assert classify_failure("review verdict recording refused: reviewer identity is a maker role") == (
            FailureKind.RECORDING_REFUSED
        )


class TestAProviderBudgetStopIsNamed:
    """A metered router refusing on its spend limit: parked, it is a window; recorded FAILED, a drained credential."""

    def test_a_terminal_budget_stop_is_an_exhausted_credential_that_halts(self) -> None:
        error = "provider_budget: insufficient_user_quota — metered provider spend limit reached"
        assert classify_failure(error) == FailureKind.CREDENTIAL_EXHAUSTED
        assert recovery_strategy(classify_failure(error)) is RecoveryStrategy.HALT

    def test_a_parked_budget_stop_is_a_usage_limit_park(self) -> None:
        error = "limit_parked: provider_budget: token cycle spend limit reached — metered provider spend limit reached"
        assert classify_failure(error) == FailureKind.USAGE_LIMIT_PARKED


class TestALeakBlockIsNeverRetried:
    """A leak block's context must never be re-sent, whatever kind the reason resolves to."""

    def test_a_leak_block_halts(self) -> None:
        reason = LimitMatch(phrase="guardrail_blocked", cause=LimitCause.LEAK_BLOCKED).as_reason()
        assert recovery_strategy(classify_failure(reason)) is RecoveryStrategy.HALT

    def test_an_egress_block_halts(self) -> None:
        reason = LimitMatch(phrase="egress_blocked", cause=LimitCause.LEAK_BLOCKED).as_reason()
        assert recovery_strategy(classify_failure(reason)) is RecoveryStrategy.HALT


class TestThePlanGateRefusalIsNamed:
    """#4578: the refusal was ``unclassified``, so no recovery mechanism could see it."""

    def test_the_gates_own_prefix_classifies_as_plan_missing(self) -> None:
        """The drift detector: ``_MATCHERS`` spells the prefix literally, like its siblings."""
        assert classify_failure(f"{PLAN_MISSING_PREFIX}refusing to dispatch t3:coder for ticket 7 (coding)") == (
            FailureKind.PLAN_MISSING
        )

    def test_it_is_never_auto_reopened(self) -> None:
        """Re-running the SAME implementing phase reproduces it; the remedy is a different phase."""
        assert recovery_strategy(FailureKind.PLAN_MISSING) is RecoveryStrategy.HALT

    def test_it_is_not_environmental(self) -> None:
        assert is_environmental(FailureKind.PLAN_MISSING) is False


class TestTheUnrecordableReviewRefusalIsNamed:
    """The refusal was ``unclassified``, so nothing could recover from it.

    ``stall_kinds`` drops an UNNAMED kind, so the re-dispatch budget never saw two in a row.
    """

    _REFUSAL = (
        f"{REVIEW_UNRECORDABLE_PREFIX}refusing to dispatch the reviewer for souliane/teatree#4225 — "
        "no pull request head is recorded for it"
    )

    def test_the_gates_own_prefix_classifies_as_review_unrecordable(self) -> None:
        assert classify_failure(self._REFUSAL) == FailureKind.REVIEW_UNRECORDABLE

    def test_the_recorders_post_hoc_refusal_classifies_identically(self) -> None:
        """One defect, one name: the mid-run stamp race still records through the recorder."""
        post_hoc = (
            f"{REVIEW_UNRECORDABLE_PREFIX}review verdict cannot be persisted: this review is answerable "
            "for souliane/teatree#4225 but no pull request head is recorded for it"
        )
        assert classify_failure(post_hoc) == FailureKind.REVIEW_UNRECORDABLE

    def test_it_is_never_auto_reopened(self) -> None:
        """Nothing a reviewer does supplies a head it was never given, so a retry hits the same wall."""
        assert recovery_strategy(FailureKind.REVIEW_UNRECORDABLE) is RecoveryStrategy.HALT

    def test_it_is_not_environmental(self) -> None:
        assert is_environmental(FailureKind.REVIEW_UNRECORDABLE) is False

    def test_two_consecutive_refusals_reach_the_stall_check(self) -> None:
        """The whole point of naming it: an UNNAMED kind is dropped, so the budget never halted."""
        kinds = [FailureKind.REVIEW_UNRECORDABLE, FailureKind.REVIEW_UNRECORDABLE]
        assert stall_kinds(kinds) == kinds


class TestOnlyAStampedExhaustionSendsTheRetryFresh:
    """The exhaustion markers are read where the runner stamps them, never anywhere in the text.

    ``exhausted_the_conversation`` decides whether a retry drops its resume. Matching a marker
    loose in the detail hands that verdict to any transient failure whose CLI output happens to
    quote one — a test name, a grep line — and the retry loses a conversation it should keep.
    """

    _COMPACTION = (
        f"{RESULT_ERROR_MARKER} {COMPACTION_BLOCKED_MARKER}Claude Code tried to auto-compact the run's "
        "history; the compaction was blocked and the run ended so it re-dispatches as a fresh session"
    )
    _CONTEXT_FULL = f"{RESULT_ERROR_MARKER} {CONTEXT_EXHAUSTED_MARKER} — subtype=error — prompt is too long"

    @pytest.mark.parametrize("reason", [_COMPACTION, _CONTEXT_FULL])
    def test_a_stamped_exhaustion_sends_the_retry_fresh(self, reason: str) -> None:
        assert exhausted_the_conversation(reason) is True

    @pytest.mark.parametrize(
        "detail",
        [
            f"pytest failed — tests/teatree_agents/test_compaction_guard.py::test_{CONTEXT_EXHAUSTED_MARKER}",
            f"grep -rn '{COMPACTION_BLOCKED_MARKER}' src/ exited 1",
        ],
    )
    def test_a_transient_failure_that_merely_quotes_a_marker_keeps_its_conversation(self, detail: str) -> None:
        assert exhausted_the_conversation(f"{RESULT_ERROR_MARKER} subtype=error — {detail}") is False

    def test_a_reason_the_runner_never_stamped_keeps_its_conversation(self) -> None:
        """No ``result_error:`` prefix means no run-ending result — whatever the text quotes."""
        assert exhausted_the_conversation(f"harness_crash: {CONTEXT_EXHAUSTED_MARKER} in the traceback") is False

    def test_a_blank_reason_keeps_its_conversation(self) -> None:
        assert exhausted_the_conversation("") is False


class TestTheClosedIssueRefusalIsNamed:
    """#2663: the refusal needs a NAME so the redispatch sweep can HALT on it.

    Left ``unclassified`` it would be indistinguishable from any other unnamed
    failure, and the sweep would keep re-offering a ticket whose issue the owner
    already closed — the ten-cycle loop this gate exists to stop.
    """

    def test_the_gates_own_prefix_classifies_as_issue_closed(self) -> None:
        reason = f"{ISSUE_CLOSED_PREFIX}refusing to dispatch t3:coder for ticket 7 (coding)"
        assert classify_failure(reason) == FailureKind.ISSUE_CLOSED

    def test_it_is_never_auto_reopened(self) -> None:
        """The remedy is a DECISION (reopen or ignore), and the tick is already taking it."""
        assert recovery_strategy(FailureKind.ISSUE_CLOSED) is RecoveryStrategy.HALT

    def test_it_is_not_environmental(self) -> None:
        assert is_environmental(FailureKind.ISSUE_CLOSED) is False

    def test_it_is_not_causeless_so_a_repeat_is_a_real_stall(self) -> None:
        """It names a specific cause, so two of them ARE one defect recurring."""
        assert is_causeless(FailureKind.ISSUE_CLOSED) is False
        assert stall_kinds([FailureKind.ISSUE_CLOSED, FailureKind.ISSUE_CLOSED]) == [
            FailureKind.ISSUE_CLOSED,
            FailureKind.ISSUE_CLOSED,
        ]


class TestAMovedPrHeadIsItsOwnNamedCause:
    """#4737: two legitimate pushes are two pushes, never one recurring defect.

    The refusal masks its SHAs, so every moved head fingerprints identically — which is
    how nine refused verdicts across three PRs became three permanently parked phases.
    """

    _REASON = f"{HEAD_SUPERSEDED_PREFIX}souliane/teatree#4716 advanced from bf526560 to 21023d20"

    def test_the_reason_classifies_as_head_superseded(self) -> None:
        assert classify_failure(self._REASON) == FailureKind.HEAD_SUPERSEDED

    def test_the_generic_superseded_name_does_not_claim_it(self) -> None:
        # The reasons are matched by CONTAINMENT and "head_superseded: " contains
        # "superseded: ", so matcher ORDER is the whole guard here.
        assert classify_failure(f"{SUPERSEDED_PREFIX}ticket reworked") == FailureKind.SUPERSEDED
        assert classify_failure(self._REASON) != FailureKind.SUPERSEDED

    def test_it_never_reopens_the_task_that_pinned_the_old_head(self) -> None:
        assert recovery_strategy(FailureKind.HEAD_SUPERSEDED) is RecoveryStrategy.HALT

    def test_it_is_environmental_because_nothing_about_the_work_is_at_fault(self) -> None:
        assert is_environmental(FailureKind.HEAD_SUPERSEDED)
        assert FailureKind.HEAD_SUPERSEDED in RECOVERY

    def test_two_moved_heads_are_dropped_from_the_fingerprint_stall(self) -> None:
        fingerprint = "b0dff8325c76"
        pairs = [(FailureKind.HEAD_SUPERSEDED, fingerprint), (FailureKind.HEAD_SUPERSEDED, fingerprint)]

        assert stall_fingerprints(pairs) == []

    def test_two_moved_heads_are_dropped_from_the_named_cause_stall(self) -> None:
        assert stall_kinds([FailureKind.HEAD_SUPERSEDED, FailureKind.HEAD_SUPERSEDED]) == []

    def test_a_genuinely_repeating_defect_still_counts(self) -> None:
        # The control: the stall detector must not read as "nothing ever stalls".
        pairs = [(FailureKind.LANDING_UNVERIFIED, "aaaa"), (FailureKind.LANDING_UNVERIFIED, "aaaa")]

        assert stall_fingerprints(pairs) == ["aaaa", "aaaa"]

"""One failure-kind → recovery-strategy table, read by both axes (#4505).

The classification and the recovery decision used to be two hand-maintained lists that never
consulted each other: ``_ENVIRONMENTAL`` (keyed on the kind) and ``_TRANSIENT_MARKERS`` (keyed on
error text). ``harness_crash`` was environmental by classification and absent from the requeue
predicate, which dropped eleven tasks in a day. These tests pin the replacement: every kind has an
explicit strategy, the retry set is a subset of the environmental one, and the strategy a real error
string resolves to is the one the deleted text predicate produced.
"""

import pytest

from teatree.core.modelkit.task_failure_taxonomy import (
    RECOVERY,
    FailureKind,
    RecoveryStrategy,
    classify_failure,
    is_environmental,
    recovery_strategy,
)

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
        assert {kind for kind, recovery in RECOVERY.items() if recovery.environmental} == _ENVIRONMENTAL_BEFORE

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

    def test_a_harness_crash_is_retried(self) -> None:
        """The #4439 evidence: environmental by classification, dropped by the old text predicate."""
        assert classify_failure("Traceback (most recent call last):\n  File ...\nException: boom") == (
            FailureKind.HARNESS_CRASH
        )
        assert recovery_strategy(FailureKind.HARNESS_CRASH) is RecoveryStrategy.RETRY

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

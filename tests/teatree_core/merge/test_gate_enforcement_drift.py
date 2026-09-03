"""``unenforced_gate_contexts`` — a workflow GATE branch protection never requires.

The defect this pins: #4641 merged with ``module-health-gate`` FAILING on both its runs,
landing exactly the ratchet violation that gate exists to catch, because the job was never
added to this repo's branch-protection required contexts despite the workflow's own legend
promising "Gate: exits non-zero on failure -> PR cannot merge".
"""

from teatree.core.merge.gate_enforcement_drift import EXPECTED_MERGE_GATE_CONTEXTS, unenforced_gate_contexts

_ACTUAL_LIVE_REQUIRED_CONTEXTS = frozenset(
    {
        "lint",
        "test (3.13)",
        "docs-drift",
        "uv-audit",
        "sbom",
        "blueprint-cross-pr",
        "eval-gate",
        "banned-terms-tree",
        "overlay-leak-tree",
        "term-source-drift",
    }
)


class TestUnenforcedGateContexts:
    def test_the_live_required_set_reproduces_the_4641_gap(self) -> None:
        # This is the repo's ACTUAL branch-protection required-context set (read 2026-09-03) —
        # none of the three GATE jobs are in it, which is exactly how #4641 merged with
        # `module-health-gate` FAILING and landed the ratchet violation it exists to block.
        assert unenforced_gate_contexts(_ACTUAL_LIVE_REQUIRED_CONTEXTS) == EXPECTED_MERGE_GATE_CONTEXTS

    def test_every_expected_context_present_reports_nothing(self) -> None:
        fully_enforced = _ACTUAL_LIVE_REQUIRED_CONTEXTS | EXPECTED_MERGE_GATE_CONTEXTS
        assert unenforced_gate_contexts(fully_enforced) == frozenset()

    def test_a_partially_widened_set_reports_only_what_is_still_missing(self) -> None:
        partially_fixed = _ACTUAL_LIVE_REQUIRED_CONTEXTS | {"module-health-gate"}
        assert unenforced_gate_contexts(partially_fixed) == frozenset({"doc-update-gate", "e2e-no-skip-gate"})

    def test_an_unreadable_required_set_reports_nothing_rather_than_a_manufactured_finding(self) -> None:
        # None is "the probe could not answer" — the caller must not read a probe outage
        # as evidence a gate is inert.
        assert unenforced_gate_contexts(None) == frozenset()

    def test_a_context_outside_the_expected_set_is_never_named(self) -> None:
        # A non-required check that is NOT one of the three named gates (e.g. an advisory
        # lane) is irrelevant to this check and must never appear in the result.
        assert "selection-audit" not in unenforced_gate_contexts(frozenset())

"""End-to-end dogfood of the directive self-modification loop (north-star PR-8).

The capstone that proves the natural-language-directive → clean self-modification
capability works on REAL components, not fakes. Where the merged PR-6/PR-7 unit
suites drive each phase with everything injected (``SimpleNamespace`` settings, all
five ``VerifySeams`` lambdas, faked guards, a faked merge probe), this harness
inverts that: it wires the REAL settings resolution from ``ConfigSetting`` rows, the
REAL recorder gate, a REAL ``DeferredQuestion`` ratify, a REAL baseline snapshot, a
REAL ``ConfigSetting`` activation write + ``get_effective_settings`` read-back, a REAL
probe over REAL ``PullRequest`` rows, the REAL ``no_collateral_regression`` fold over
two REAL snapshots, the REAL ``CriticFinding`` count, and one REAL
``run_acceptance_tests`` subprocess.

**Isolation boundary — the pytest-django test database.** Directive writes are
transactional, and no step touches the production settings store.

**Two justified guard seams.** Only G3 (signal-trust, a healthy fixture report) and G4
(budget, ``BudgetVerdict.allow()``) stay injected, because they probe host-wide
external state (28-day signal history, a spend ledger) a hermetic test DB cannot
honestly satisfy. G2 reads unconditional critic and merge-quality enforcement.
"""

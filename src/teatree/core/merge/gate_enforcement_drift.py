"""A CI job the workflow itself labels a blocking GATE, that branch protection never requires.

``.github/workflows/ci.yml``'s own legend is explicit: "Gate: exits non-zero on failure ->
PR cannot merge." A job commented ``# GATE`` without a "NON-required" qualifier is a promise
that a red run blocks every merge — the SAME promise :mod:`teatree.core.merge.ci_rollup`
enforces for the keystone's own autonomous merges, by reading the live branch-protection
required-context set and refusing to block on anything outside it (`ci_rollup.py`'s own
docstring: "a non-required check NEVER blocks").

That promise held for ``module-health-gate``, ``doc-update-gate`` and ``e2e-no-skip-gate``
on paper only: none of the three was ever added to this repo's branch-protection required
contexts. It is not theoretical — #4641 merged with ``module-health-gate`` reporting
FAILURE on both its runs, and the merge landed exactly the ratchet violation that gate
exists to block (``hooks/scripts/hook_router.py`` grew from 36 to 37 public module-level
functions; ``scripts/eval/corpus_gen/catalog.py`` grew from 886 to 896 LOC — both already
over their module-health cap, whose ratchet only permits shrinking).

This is the ``shipped-inert`` catalog entry (a feature merged but not in force), discovered
by the periodic holistic review reading the LIVE branch-protection value rather than the
workflow file's stated intent. Fixing the branch-protection contexts themselves is a
repo-settings mutation outside this module's reach (and outside what an autonomous pass
should do without the owner's sign-off, given it changes what blocks EVERY future merge);
this module is the read-only detector so the gap stays visible instead of silently
recurring — see :mod:`teatree.cli.doctor.checks_merge_gate_enforcement` for the caller.
"""

#: CI job/context names this repo's ``.github/workflows/ci.yml`` legend declares a
#: blocking GATE ("PR cannot merge" on failure) with no "NON-required" qualifier —
#: unlike, e.g., ``selection-audit``, which the workflow's own comment marks
#: "GATE (PR-only, NON-required)". Each name here is the exact GitHub check context
#: branch protection would need to list for the workflow's own stated contract to hold.
EXPECTED_MERGE_GATE_CONTEXTS: frozenset[str] = frozenset(
    {
        "module-health-gate",
        "doc-update-gate",
        "e2e-no-skip-gate",
    }
)


def unenforced_gate_contexts(required_contexts: frozenset[str] | set[str]) -> frozenset[str]:
    """:data:`EXPECTED_MERGE_GATE_CONTEXTS` entries absent from the live *required_contexts*."""
    return frozenset(EXPECTED_MERGE_GATE_CONTEXTS - set(required_contexts))

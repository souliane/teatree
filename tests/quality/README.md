# tests/quality — cross-cutting tree gates

This directory holds **cross-cutting quality gates**: tests that assert a
property over the whole `src/teatree` tree (or the repo layout) rather than
exercising one module. Examples here: import-direction contracts, chokepoint
enforcement, dead-plan-ref scans, mutation runs, the process-cache reset roster,
and other AST/lint tree-walks.

A test here is not the path-mirror of any single `src/teatree/<pkg>/<mod>.py`, so
it carries a `# test-path: cross-cutting` pragma to tell the test-path-mirror gate
(`src/teatree/quality/test_path_mirror.py`) it is intentionally not mirrored.

This directory is **CI-only**: it runs whole-tree in the `test (3.13)` shard and
is deselected from the fast local lane (it took ~420s locally). Some gates are
additionally marked `@pytest.mark.push_heavy`.

## What belongs where

| Test shape | Directory |
| --- | --- |
| Property over the whole `src/teatree` tree / repo layout (AST walk, import contract, chokepoint) | `tests/quality` (this dir), with `# test-path: cross-cutting` |
| The path-mirror of one `src/teatree/quality/<mod>.py` module | `tests/teatree_quality` |

## Grandfathered mirror tests living here

Eight tests exercise a specific `teatree.quality.<mod>` module (so their mirror
home is `tests/teatree_quality`) yet live here — `test_catalog.py`,
`test_chokepoints.py`, `test_mutation.py`, `test_mutation_kill_proof.py`,
`test_mutation_run.py`, `test_patch_targets_resolve.py`, `test_regression_rules.py`,
`test_test_shape.py`. They are recorded in `tests/test_path_mirror_grandfathered.txt`
rather than moved: several are heavy (`push_heavy` mutation/regression runs with
long timeouts) and the whole `tests/quality` lane is CI-only, so keeping them in
this slow lane is deliberate. Move one only alongside a check that its lane/timing
placement still holds, and drop its ledger line in the same change.

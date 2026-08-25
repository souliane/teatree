# tests/teatree_quality — mirror of src/teatree/quality

This directory is the **path-mirror** of the `src/teatree/quality/` module. A test
here exercises one specific `src/teatree/quality/<mod>.py` and mirrors its path:
`tests/teatree_quality/test_<mod>.py` tests `teatree.quality.<mod>` (e.g.
`test_debt_delta.py` ↔ `teatree.quality.debt_delta`,
`test_test_path_mirror.py` ↔ `teatree.quality.test_path_mirror`). The
test-path-mirror gate (`src/teatree/quality/test_path_mirror.py`) enforces this
mapping.

Do not put whole-tree/cross-cutting gates here — those go in `tests/quality`
(with a `# test-path: cross-cutting` pragma). A mirror test here may still READ
the whole tree, because the module it mirrors scans it — which is why this dir is
one of the affected-lane's `FLOOR_DIRS` (`teatree.quality.affected_tests`): an
import graph cannot decide such a test unaffected by any diff. The two directories read similarly
but answer different questions:

| Test shape | Directory |
| --- | --- |
| The path-mirror of one `src/teatree/quality/<mod>.py` module | `tests/teatree_quality` (this dir) |
| A property over the whole `src/teatree` tree / repo layout | `tests/quality`, with `# test-path: cross-cutting` |

Some mirror tests of `teatree.quality.*` modules currently live in `tests/quality`
instead (they are heavy / CI-only) and are grandfathered in
`tests/test_path_mirror_grandfathered.txt`; see `tests/quality/README.md`.

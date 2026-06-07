"""Manual-stage hook: the test-files-mirror-src forward-guard.

Thin wrapper over :mod:`teatree.quality.test_path_mirror` — the analysis and the
ratchet decision live there, read from ``[tool.teatree.test_path_mirror]`` in
``pyproject.toml``. Registered ``stages: [manual]`` so it cannot wedge a commit
while the baseline is still high; CI runs the same check via ``t3 tool
test-path-mirror``. Promote to ``stages: [push]`` once the relocation sweep has
ratcheted the baseline low.

This mirrors ``check_test_shape.py``: the report is informational, the gate
promotion is a per-repo config + stage choice, not a baked-in commit block.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from teatree.quality.test_path_mirror import build_report, load_config

    config = load_config(_REPO_ROOT / "pyproject.toml")
    report = build_report(root=_REPO_ROOT, config=config)

    if not report.exceeds_baseline:
        print(
            f"check_test_path_mirror: {report.live_count} violation(s) at/below baseline "
            f"{report.baseline} (test files mirror src — ratchet holds)."
        )
        return 0

    print(f"Test-path-mirror REGRESSION: {report.live_count} violation(s) exceed baseline {report.baseline}:")
    print()
    for line in report.summary_lines():
        print(line)
    print()
    print(
        "A test file must mirror its src/teatree/<pkg>/... module path as tests/teatree_<pkg>/... . "
        "Move the new file, or (for a genuine multi-package contract test) add a "
        "`# test-path: cross-cutting` pragma."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

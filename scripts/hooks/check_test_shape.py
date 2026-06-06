"""Manual-stage hook: the conservative, report-first test-shape check.

Thin wrapper over :mod:`teatree.quality.test_shape` — the analysis and the
``warn``/``block`` mode decision live there, read from
``[tool.teatree.test_shape]`` in ``pyproject.toml``. Registered ``stages:
[manual]`` so it is advisory by default and can never wedge a commit; CI runs
the same check via ``t3 tool test-shape``. When the configured mode is
``block`` the wrapper still exits non-zero so a deliberate opt-in is honoured.

This mirrors ``check_antipatterns.py``: the report is informational, the gate
promotion is a per-repo config choice, not a baked-in commit block.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from teatree.quality.test_shape import Mode, build_report, collect_source_files, collect_test_files, load_config

    config = load_config(_REPO_ROOT / "pyproject.toml")
    report = build_report(
        test_files=collect_test_files(_REPO_ROOT),
        source_files=collect_source_files(_REPO_ROOT),
        config=config,
        root=_REPO_ROOT,
    )

    if not report.has_findings:
        print("check_test_shape: no findings (ratio at/above baseline, no unparametrized duplicates).")
        return 0

    severity = "BLOCK" if report.mode is Mode.BLOCK else "WARN (advisory)"
    print(f"Test-shape findings [{severity}]:")
    print()
    for line in report.summary_lines():
        print(line)
    print()
    if report.mode is Mode.WARN:
        print('Advisory only. Set [tool.teatree.test_shape] mode = "block" in pyproject.toml to enforce.')
    return 1 if report.should_block else 0


if __name__ == "__main__":
    raise SystemExit(main())

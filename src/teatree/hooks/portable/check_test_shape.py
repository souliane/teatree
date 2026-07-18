"""Portable hook: the conservative, report-first test-shape check.

Thin wrapper over :mod:`teatree.quality.test_shape` — the analysis and the
``warn``/``block`` mode decision live there, read from
``[tool.teatree.test_shape]`` in the *current repo's* ``pyproject.toml``. The
root is the current working directory (``git`` invokes hooks from the repo
root), so the same gate runs unchanged in any consuming repo via
``t3 hook run check_test_shape``. When the configured mode is ``block`` the
wrapper exits non-zero so a deliberate opt-in is honoured.
"""

from pathlib import Path

from teatree.quality.test_shape import Mode, build_report, collect_source_files, collect_test_files, load_config


def main() -> int:
    root = Path.cwd()
    config = load_config(root / "pyproject.toml")
    report = build_report(
        test_files=collect_test_files(root),
        source_files=collect_source_files(root),
        config=config,
        root=root,
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

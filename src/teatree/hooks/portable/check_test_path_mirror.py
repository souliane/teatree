"""Portable hook: the test-files-mirror-src forward-guard.

Thin wrapper over :mod:`teatree.quality.test_path_mirror` — the analysis and the
ratchet decision live there, read from ``[tool.teatree.test_path_mirror]`` in
the *current repo's* ``pyproject.toml``. The root is the current working
directory (``git`` invokes hooks from the repo root), so the same gate runs
unchanged in any consuming repo via ``t3 hook run check_test_path_mirror``.
"""

from pathlib import Path

from teatree.quality.test_path_mirror import build_report, load_config


def main() -> int:
    root = Path.cwd()
    config = load_config(root / "pyproject.toml")
    report = build_report(root=root, config=config)

    if not report.failed:
        print(
            f"check_test_path_mirror: {report.live_count} grandfathered violation(s), ledger exact "
            "(test files mirror src — ratchet holds)."
        )
        return 0

    if report.unknown_violations:
        print(f"Test-path-mirror REGRESSION: {len(report.unknown_violations)} new mis-pathed test file(s):")
        print()
        for line in report.summary_lines():
            print(line)
        print()
        print(
            "A test file must mirror its src/teatree/<pkg>/... module path as tests/teatree_<pkg>/... . "
            "Move the new file, or (for a genuine multi-package contract test) add a "
            "`# test-path: cross-cutting` pragma."
        )
    if report.stale_entries:
        print(f"Test-path-mirror STALE LEDGER: {len(report.stale_entries)} entry(ies) no longer violate:")
        print()
        for line in report.stale_lines():
            print(line)
        print()
        print("Bank the reduction: remove the stale line(s), or run `t3 tool test-path-mirror --update-baseline`.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

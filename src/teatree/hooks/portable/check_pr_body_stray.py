"""Portable hook: refuse a staged PR/MR body scratch file.

Reads ``git diff --cached --name-only`` in the current repo and fails when any
staged path is a hand-named ``pr-body.*`` / ``pr_body.*`` file — the scratch body
belongs in a system temp file (:func:`teatree.utils.pr_body.pr_body_tempfile`),
never committed. Pure name check, no Django/DB; runs unchanged in any checkout
via ``t3 hook run check_pr_body_stray``.
"""

from teatree.quality.pr_body_stray import block_message, stray_pr_body_paths
from teatree.utils.run import run_allowed_to_fail


def _staged_paths() -> list[str]:
    result = run_allowed_to_fail(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        expected_codes=None,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    stray = stray_pr_body_paths(_staged_paths())
    if not stray:
        return 0
    print(block_message(stray))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

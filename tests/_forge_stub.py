"""Shared test-infra helper: the changed-files answer every merge-path ``gh`` stub owes.

The substrate detector reads the PR's changed paths at the merge chokepoint and holds
the merge when that list comes back empty — a real open PR always changes at least one
file, so ``[]`` is a failed read, not a proven non-substrate diff. A stub whose
fall-through returns an empty body therefore turns every merge test into a substrate
hold. Route the fall-through through :func:`changed_files_stdout` so the rule lives in
one place instead of once per stub.
"""

_CHANGED_FILES_ENDPOINT = "/files"


def changed_files_stdout(joined_argv: str, *, otherwise: str = "") -> str:
    """One changed path when *joined_argv* is the changed-files read, else *otherwise*."""
    return "README.md\n" if _CHANGED_FILES_ENDPOINT in joined_argv else otherwise

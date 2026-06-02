"""Pre-push hook: refuse a diff that adds comments restating the code.

The commit-side enforcement of the near-zero-comments rule (names + types
are the documentation). Thin wrapper over
:mod:`teatree.hooks.privacy_diff_comment_density` — the parsing, thresholds,
and pragma/docstring/license exemptions live there, shared with the standalone
``t3 tool comment-density`` command and the CI job so there is one source of
truth.

Scans ``git diff --cached`` (the staged contents about to be pushed). On a
clean diff the hook is silent and exits 0. On a comment-dense file it prints
the per-file finding and exits 1. Tests and docs are exempt; tooling pragmas
(``# noqa`` / ``# type:`` / ``// eslint-disable`` …), docstrings, and
license/shebang headers do not count toward the comment tally.
"""

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _staged_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "--cached", "--diff-filter=ACMR", "-U0"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def main() -> int:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from teatree.hooks.privacy_diff_comment_density import report_diff

    diff = _staged_diff()
    if not diff:
        return 0

    findings = report_diff(diff)
    if not findings:
        return 0

    print("comment-density gate (near-zero-comments rule — names + types are the docs):\n")
    for finding in findings:
        print(f"  - {finding.render()}")
    print(
        "\nThese added comments restate what the code already says. Delete the\n"
        "WHAT-narration, or rename the symbols so the intent is self-evident.\n"
        "Genuine rationale belongs in the commit message; tooling directives\n"
        "(# noqa / # type: / // eslint-disable …) are already exempt."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

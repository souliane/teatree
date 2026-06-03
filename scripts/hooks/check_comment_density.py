"""Pre-push hook: warn on a diff that adds comments restating the code.

The commit-side surface of the near-zero-comments rule (names + types are
the documentation). Thin wrapper over
:mod:`teatree.hooks.privacy_diff_comment_density` — the parsing, thresholds,
and pragma/docstring/license exemptions live there, shared with the standalone
``t3 tool comment-density`` command and the CI job so there is one source of
truth.

The check is **advisory**: scans ``git diff --cached`` (the staged contents
about to be pushed) and prints the per-file finding as a warning, but
**always exits 0** so it never blocks the push. There is no content-blind
heuristic for "overly long prose" that does not also flag legitimate long
comments, so the signal is surfaced without failing. Tests and docs are
exempt; tooling pragmas (``# noqa`` / ``# type:`` / ``// eslint-disable`` …),
docstrings, and license/shebang headers do not count toward the comment tally.
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

    print("comment-density warning (near-zero-comments rule — names + types are the docs):\n")
    for finding in findings:
        print(f"  - {finding.render()}")
    print(
        "\nThese added comments may restate what the code already says. Consider\n"
        "deleting the WHAT-narration, or renaming the symbols so the intent is\n"
        "self-evident. Genuine rationale belongs in the commit message; tooling\n"
        "directives (# noqa / # type: / // eslint-disable …) are already exempt.\n"
        "This is advisory only — the push is not blocked."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

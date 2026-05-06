"""Commit-msg hook: warn when src/ changes but BLUEPRINT.md does not.

Exits non-zero when source code changes without a corresponding BLUEPRINT.md
update, unless the commit type is one that typically doesn't require it
(test, docs, style, chore, ci, fix).

See: souliane/teatree#8
"""

import pathlib
import subprocess
import sys

# Commit types that don't require BLUEPRINT updates.
_EXEMPT_PREFIXES = ("test", "docs", "style", "chore", "ci", "fix")


def _staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip().splitlines()


def _commit_message() -> str:
    min_args = 2
    if len(sys.argv) < min_args:
        return ""
    try:
        return pathlib.Path(sys.argv[1]).open(encoding="utf-8").readline().strip()
    except OSError:
        return ""


def main() -> int:
    msg = _commit_message()

    # Skip for commit types that don't need blueprint changes.
    msg_lower = msg.lower()
    if any(msg_lower.startswith((f"{prefix}:", f"{prefix}(")) for prefix in _EXEMPT_PREFIXES):
        return 0

    files = _staged_files()
    has_src = any(f.startswith("src/") for f in files)
    has_blueprint = "BLUEPRINT.md" in files

    if has_src and not has_blueprint:
        print()
        print("  BLUEPRINT.md reminder:")
        print()
        print("    Source code changed but BLUEPRINT.md was not updated.")
        print("    If this change adds/removes/renames endpoints, models,")
        print("    commands, or config, please update BLUEPRINT.md too.")
        print()
        print("    To skip: use a fix/test/docs/style/chore/ci commit type.")
        print()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

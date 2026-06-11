"""Commit-msg hook: warn when src/ changes but the BLUEPRINT does not.

Exits non-zero when source code changes without a corresponding BLUEPRINT
update, unless the commit type is one that typically doesn't require it
(test, docs, style, chore, ci, fix, refactor).

The "BLUEPRINT" is the top-level ``BLUEPRINT.md`` plus its split appendix
files under ``docs/blueprint/`` (e.g. ``configuration.md``,
``loop-topology.md``) — the appendices ARE the BLUEPRINT, so updating one of
them satisfies the sync requirement just as the monolith does.

See: souliane/teatree#8
"""

import pathlib
import subprocess
import sys

# Commit types that don't require BLUEPRINT updates. ``refactor`` joins the
# set because a behaviour-preserving internal change (a swapped runner, an
# extracted helper) does not alter the external contracts BLUEPRINT documents
# — the same reasoning that exempts ``fix``.
_EXEMPT_PREFIXES = ("test", "docs", "style", "chore", "ci", "fix", "refactor")


def _staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip().splitlines()


def _is_blueprint(path: str) -> bool:
    """A staged path that counts as a BLUEPRINT update.

    The top-level ``BLUEPRINT.md`` or any of its split appendix markdown files
    under ``docs/blueprint/`` — the appendices are the BLUEPRINT's deep-mechanics
    sections, so editing one satisfies the sync requirement.
    """
    return path == "BLUEPRINT.md" or (path.startswith("docs/blueprint/") and path.endswith(".md"))


def _commit_message() -> str:
    min_args = 2
    if len(sys.argv) < min_args:
        return ""
    try:
        with pathlib.Path(sys.argv[1]).open(encoding="utf-8") as fh:
            return fh.readline().strip()
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
    has_blueprint = any(_is_blueprint(f) for f in files)

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

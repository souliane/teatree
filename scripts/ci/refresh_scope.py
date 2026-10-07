"""Whether a lock-refresh branch still changes only lock/generated artifacts (#4569).

``lock_delta.py`` ranks VERSION moves and cannot see PATHS, so source pushed onto a
refresh branch reads as a patch-level bump and self-merges on green. Exit 0: every
changed path is allowed. Exit 1: something else changed, printed. Exit 2: the diff
could not be read or is empty, so nothing vouches for a merge.

Stdlib only: the refresh job runs no ``uv sync``.
"""

import argparse
import subprocess
import sys
from collections.abc import Iterable, Sequence

ALLOWED_FILES = frozenset({"uv.lock", "dist/sbom.json"})
ALLOWED_PREFIXES = ("docs/generated/",)


def out_of_scope(paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if path not in ALLOWED_FILES and not path.startswith(ALLOWED_PREFIXES))


def changed_paths(base: str, head: str) -> list[str]:
    # --no-renames: a rename INTO an allowed dir must still report the source path it removed.
    diff = subprocess.run(
        ["git", "diff", "--name-only", "-z", "--no-renames", f"{base}...{head}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in diff.stdout.split("\0") if path]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    try:
        paths = changed_paths(args.base, args.head)
    except subprocess.CalledProcessError as exc:
        print(
            f"::error::Could not read the refresh diff {args.base}...{args.head}: {exc.stderr.strip()}", file=sys.stderr
        )
        return 2
    if not paths:
        print(f"::error::The refresh diff {args.base}...{args.head} is empty.", file=sys.stderr)
        return 2
    escaping = out_of_scope(paths)
    if not escaping:
        print(f"In scope: all {len(paths)} changed path(s) are lock/generated artifacts.")
        return 0
    print(f"Out of scope: {len(escaping)} of {len(paths)} changed path(s) are not lock/generated artifacts:")
    for path in escaping:
        print(f"  {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

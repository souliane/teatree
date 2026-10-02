"""Print the hand-written LoC delta between two refs; advisory, so it always exits 0."""

import argparse
from pathlib import Path

from teatree.quality.hand_written_loc import diff_range_loc
from teatree.utils.run import CommandFailedError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("head")
    args = parser.parse_args()
    try:
        print(diff_range_loc(args.base, args.head, repo=Path.cwd()))
    except (OSError, CommandFailedError) as exc:
        print(f"hand-written LoC unavailable: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

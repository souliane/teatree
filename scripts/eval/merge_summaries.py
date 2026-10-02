r"""Merge the weekly metered eval's per-shard sanitized summaries into one dashboard.

The weekly workflow fans out across ``{lane, shard}`` legs; each leg uploads its
own sanitized ``--summary-md`` markdown (counts + a ``scenario | lane | verdict |
trials | cost`` table, NO transcript). This script is the host-workflow shim
around the merge: it reads the per-shard summary files (a directory or explicit
paths) and delegates to :func:`teatree.eval.summaries.merge_summaries`, the
shared core also exposed as ``t3 eval merge-summaries`` for overlays to reuse.

The run-url / sha / generated-at are PASSED IN (the timestamp is never
``datetime.now()`` here, so the merge is deterministic and unit-testable). Only
the publish-safe summary rows are read — the transcript never enters here, so the
merged dashboard is safe to commit and serve on Pages.
"""

import argparse
import sys
from pathlib import Path

from teatree.eval.summaries import merge_summaries

__all__ = ["main"]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="Per-shard summary .md files, or a directory of them.")
    parser.add_argument("--run-url", required=True, help="The workflow run URL (injected by the workflow).")
    parser.add_argument("--sha", required=True, help="The commit SHA the run measured (injected).")
    parser.add_argument("--generated-at", required=True, help="ISO-8601 timestamp (injected; never computed here).")
    parser.add_argument("--out", default=None, help="Write the dashboard to this path instead of stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    dashboard = merge_summaries(args.inputs, run_url=args.run_url, sha=args.sha, generated_at=args.generated_at)
    if args.out is not None:
        Path(args.out).write_text(dashboard, encoding="utf-8")
    else:
        print(dashboard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

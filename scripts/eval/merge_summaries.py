r"""Merge the weekly metered eval's per-shard sanitized summaries into one dashboard.

The weekly workflow fans out across ``{lane, shard}`` legs; each leg uploads its
own sanitized ``--summary-md`` markdown (counts + a ``scenario | lane | verdict |
trials`` table, NO transcript). This script reads every per-shard summary file in
a directory (or the explicit file paths given) and emits ONE combined dashboard
to stdout (or ``--out``):

a title; a run line carrying the ``--run-url`` / ``--sha`` / ``--generated-at``
the workflow injects (the timestamp is PASSED IN, never ``datetime.now()`` here,
so the script is deterministic and unit-testable); summed PASS / FAIL / skip
totals across every shard; the merged per-scenario table sorted by lane then
name; and a final line linking the run for the PRIVATE per-trial transcripts.

Only the publish-safe summary rows are read — the transcript never enters here,
so the merged dashboard is safe to commit and serve on Pages.
"""

import argparse
import dataclasses
import sys
from collections.abc import Iterable
from pathlib import Path

_TABLE_HEADER = "| scenario | lane | verdict | trials |"
_TABLE_COLUMNS = ("scenario", "lane", "verdict", "trials")


@dataclasses.dataclass(frozen=True)
class SummaryRow:
    scenario: str
    lane: str
    verdict: str
    trials: str


def _parse_rows(markdown: str) -> list[SummaryRow]:
    rows: list[SummaryRow] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != len(_TABLE_COLUMNS):
            continue
        if tuple(cells) == _TABLE_COLUMNS or set(cells[0]) <= {"-"}:
            continue
        rows.append(SummaryRow(scenario=cells[0], lane=cells[1], verdict=cells[2], trials=cells[3]))
    return rows


def _shard_files(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.md")))
        elif path.is_file():
            paths.append(path)
    return paths


def merge(files: Iterable[Path]) -> list[SummaryRow]:
    rows: list[SummaryRow] = []
    for path in files:
        rows.extend(_parse_rows(path.read_text(encoding="utf-8")))
    return sorted(rows, key=lambda row: (row.lane, row.scenario))


def render_dashboard(rows: list[SummaryRow], *, run_url: str, sha: str, generated_at: str) -> str:
    passed = sum(1 for row in rows if row.verdict == "pass")
    failed = sum(1 for row in rows if row.verdict == "fail")
    skipped = sum(1 for row in rows if row.verdict == "skip")
    table = [
        _TABLE_HEADER,
        "| --- | --- | --- | --- |",
        *(f"| {row.scenario} | {row.lane} | {row.verdict} | {row.trials} |" for row in rows),
    ]
    return "\n".join(
        [
            "# Weekly behavioral-eval dashboard",
            "",
            f"Run [{sha}]({run_url}) · generated at {generated_at}",
            "",
            f"**{passed} passed**, **{failed} failed**, **{skipped} skipped** (of {len(rows)})",
            "",
            *table,
            "",
            f"The private per-trial transcripts are the `eval-report-*` artifacts on [the run]({run_url}).",
            "",
        ]
    )


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
    rows = merge(_shard_files(args.inputs))
    dashboard = render_dashboard(rows, run_url=args.run_url, sha=args.sha, generated_at=args.generated_at)
    if args.out is not None:
        Path(args.out).write_text(dashboard, encoding="utf-8")
    else:
        print(dashboard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

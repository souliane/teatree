r"""Merge the weekly metered eval's per-shard sanitized summaries into one dashboard.

The weekly workflow fans out across ``{lane, shard}`` legs; each leg uploads its
own sanitized ``--summary-md`` markdown (counts + a ``scenario | lane | verdict |
trials | cost`` table, NO transcript). This module reads every per-shard summary
file in a directory (or the explicit file paths given) and renders ONE combined
dashboard:

a title; a run line carrying the ``run_url`` / ``sha`` / ``generated_at`` the
workflow injects (the timestamp is PASSED IN, never ``datetime.now()`` here, so
the merge is deterministic and unit-testable); summed PASS / FAIL / skip totals
AND total metered cost across every shard; the merged per-scenario table (with
per-scenario cost) sorted by lane then name; and a final line linking the run for
the PRIVATE per-trial transcripts.

Only the publish-safe summary rows are read — the transcript never enters here,
so the merged dashboard is safe to commit and serve on Pages.

This is the shared core behind both ``t3 eval merge-summaries`` (the reusable
overlay-facing CLI) and ``scripts/eval/merge_summaries.py`` (the thin script the
host workflow shells out to). Both delegate here; the logic lives once.
"""

import dataclasses
from collections.abc import Iterable
from pathlib import Path

_TABLE_HEADER = "| scenario | lane | verdict | trials | cost |"
_TABLE_COLUMNS = ("scenario", "lane", "verdict", "trials", "cost")


@dataclasses.dataclass(frozen=True)
class SummaryRow:
    scenario: str
    lane: str
    verdict: str
    trials: str
    cost: str
    source: str = ""


def _parse_cost(cell: str) -> float:
    try:
        return float(cell.strip().lstrip("$"))
    except ValueError:
        return 0.0


def _parse_rows(markdown: str) -> list[SummaryRow]:
    rows: list[SummaryRow] = []
    sources: dict[str, str] = {}
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and ": key `" in stripped:
            name, _, _ = stripped[2:].partition(": key `")
            sources[name] = stripped
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != len(_TABLE_COLUMNS):
            continue
        if tuple(cells) == _TABLE_COLUMNS or set(cells[0]) <= {"-"}:
            continue
        rows.append(SummaryRow(scenario=cells[0], lane=cells[1], verdict=cells[2], trials=cells[3], cost=cells[4]))
    return [dataclasses.replace(row, source=sources.get(row.scenario, "")) for row in rows]


def shard_files(inputs: list[str]) -> list[Path]:
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
    incomplete = sum(1 for row in rows if row.verdict == "incomplete")
    cached = sum(1 for row in rows if row.verdict == "cached pass")
    total_cost = sum(_parse_cost(row.cost) for row in rows)
    table = [
        _TABLE_HEADER,
        "| --- | --- | --- | --- | --- |",
        *(f"| {row.scenario} | {row.lane} | {row.verdict} | {row.trials} | {row.cost} |" for row in rows),
    ]
    sources = [row.source for row in rows if row.source]
    return "\n".join(
        [
            "# Weekly behavioral-eval dashboard",
            "",
            f"Run [{sha}]({run_url}) · generated at {generated_at}",
            "",
            (
                f"**{passed} passed**, **{failed} failed**, **{skipped} skipped**, "
                f"**{incomplete} incomplete**, **{cached} cached** (of {len(rows)}) "
                f"· total cost ${total_cost:.4f}"
            ),
            "",
            *table,
            "",
            *sources,
            *([""] if sources else []),
            f"The private per-trial transcripts are the `eval-report-*` artifacts on [the run]({run_url}).",
            "",
        ]
    )


def merge_summaries(
    inputs: list[str],
    *,
    run_url: str,
    sha: str,
    generated_at: str,
) -> str:
    return render_dashboard(merge(shard_files(inputs)), run_url=run_url, sha=sha, generated_at=generated_at)

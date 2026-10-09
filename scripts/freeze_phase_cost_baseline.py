"""Emit (or verify) the committed pre-cutover per-phase cost baseline.

Reads the control DB strictly read-only (``mode=ro`` + ``PRAGMA query_only``) and
writes ``src/teatree/core/cost_baseline/pre_opus5.yaml``. Because every row is
filtered to ``started_at < --cutover-at``, a later run reproduces byte-identical
output no matter how many post-cutover attempts have since landed — which is what
makes ``--check`` a durable proof that the committed freeze is faithful rather
than a one-shot snapshot nobody can re-derive.

``teatree_taskattempt`` is ~99.6% park-audit rows (``limit_parked:`` /
``stuck_loop:``) carrying no model and no tokens, so the real-dispatch filter is
load-bearing: without it every figure below measures the park loop. The three
candidate filters are counted separately and written into ``coverage`` so the
choice of ``output_tokens IS NOT NULL`` is evidenced in the artifact itself.

    uv run python scripts/freeze_phase_cost_baseline.py --check
"""

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from teatree.core.cost_baseline.aggregate import AttemptRecord, UsageStats, aggregate_by_phase, aggregate_by_phase_model
from teatree.core.cost_baseline.frozen import FROZEN_BASELINE_PATH

#: The cutover this baseline is frozen against — the change that repointed
#: ``TIER_MODELS["frontier"]``, recorded as provenance in the artifact.
CUTOVER_MODEL = "claude-opus-5"
CUTOVER_PULL_REQUEST = 3731
CUTOVER_MERGED_AT = "2026-07-25T08:59:42Z"

_DEFAULT_DB = Path.home() / ".local" / "share" / "teatree" / "db.sqlite3"

_ATTEMPTS_SQL = """
SELECT t.phase, a.model, a.input_tokens, a.output_tokens, a.cache_read_tokens,
a.cache_write_tokens, a.num_turns, a.cost_usd, a.started_at
FROM teatree_taskattempt a
JOIN teatree_task t ON t.id = a.task_id
WHERE a.output_tokens IS NOT NULL AND a.started_at < ?
"""


def _read_only(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=1")
    return connection


def _scalar(connection: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> int:
    return int(connection.execute(sql, tuple(params)).fetchone()[0])


def _fetch_records(connection: sqlite3.Connection, cutover_at: str) -> tuple[list[AttemptRecord], str, str]:
    rows = connection.execute(_ATTEMPTS_SQL, (cutover_at,)).fetchall()
    if not rows:
        msg = f"no real dispatch attempts before {cutover_at} — refusing to freeze an empty baseline"
        raise SystemExit(msg)
    records = [
        AttemptRecord(
            phase=row[0],
            model=row[1],
            input_tokens=row[2],
            output_tokens=row[3],
            cache_read_tokens=row[4],
            cache_write_tokens=row[5],
            num_turns=row[6],
            cost_usd=row[7],
        )
        for row in rows
    ]
    started = sorted(row[8] for row in rows)
    return records, started[0], started[-1]


_TOTAL_ROWS_SQL = "SELECT count(*) FROM teatree_taskattempt"
_CARRIES_MODEL_SQL = "SELECT count(*) FROM teatree_taskattempt WHERE model != '' AND started_at < ?"
_HAS_COST_SQL = "SELECT count(*) FROM teatree_taskattempt WHERE cost_usd IS NOT NULL AND started_at < ?"
_HAS_OUTPUT_SQL = "SELECT count(*) FROM teatree_taskattempt WHERE output_tokens IS NOT NULL AND started_at < ?"
_POST_CUTOVER_SQL = "SELECT count(*) FROM teatree_taskattempt WHERE output_tokens IS NOT NULL AND started_at >= ?"


def _coverage(connection: sqlite3.Connection, cutover_at: str) -> dict[str, int]:
    """The three real-dispatch predicates, counted separately so their agreement is evidence."""
    at = (cutover_at,)
    return {
        "taskattempt_rows_total": _scalar(connection, _TOTAL_ROWS_SQL),
        "real_dispatch_rows": _scalar(connection, _CARRIES_MODEL_SQL, at),
        "rows_with_cost_usd": _scalar(connection, _HAS_COST_SQL, at),
        "rows_with_output_tokens": _scalar(connection, _HAS_OUTPUT_SQL, at),
        "post_cutover_rows": _scalar(connection, _POST_CUTOVER_SQL, at),
    }


def _rounded(stats: UsageStats) -> dict[str, float | int]:
    return {name: value if name == "attempts" else round(float(value), 4) for name, value in asdict(stats).items()}


def render_baseline(db: Path, *, cutover_at: str) -> str:
    """Build the frozen-baseline YAML text from a read-only pass over the control DB."""
    connection = _read_only(db)
    try:
        records, first_started, last_started = _fetch_records(connection, cutover_at)
        coverage = _coverage(connection, cutover_at)
    finally:
        connection.close()
    per_phase_model: dict[str, dict[str, dict[str, float | int]]] = {}
    for (phase, model), stats in aggregate_by_phase_model(records).items():
        per_phase_model.setdefault(phase, {})[model] = _rounded(stats)
    document = {
        "cutover": {
            "model": CUTOVER_MODEL,
            "pull_request": CUTOVER_PULL_REQUEST,
            "merged_at": CUTOVER_MERGED_AT,
        },
        "window": {
            "first_attempt_started_at": first_started,
            "last_attempt_started_at": last_started,
        },
        "coverage": coverage,
        "per_phase": {phase: _rounded(stats) for phase, stats in aggregate_by_phase(records).items()},
        "per_phase_model": per_phase_model,
    }
    return _HEADER + yaml.safe_dump(document, sort_keys=True, default_flow_style=False)


_HEADER = """\
# FROZEN pre-cutover per-phase cost baseline — do not regenerate against a
# post-cutover window.
#
# Every figure below is measured over REAL agent dispatches only (see
# `coverage`), started strictly before the `cutover` PR merged. It is committed
# rather than queried because a table holding both populations separates them by
# nothing it records.
#
# Regenerate/verify with:
#   uv run python scripts/freeze_phase_cost_baseline.py --check
#
# Token figures are counts; `*_cost_usd` are US dollars. `billed_input` is
# prompt + cache-read + cache-write. `median`/`p95` are nearest-rank, so each is
# an observation some attempt actually recorded.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB, help="control DB (read-only)")
    parser.add_argument("--out", type=Path, default=FROZEN_BASELINE_PATH, help="baseline file to write")
    parser.add_argument(
        "--cutover-at",
        default=CUTOVER_MERGED_AT.replace("T", " ").rstrip("Z"),
        help="attempts started at/after this DB timestamp are excluded",
    )
    parser.add_argument("--check", action="store_true", help="verify the committed file instead of writing it")
    args = parser.parse_args(argv)
    rendered = render_baseline(args.db, cutover_at=args.cutover_at)
    if not args.check:
        args.out.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.out}")
        return 0
    if not args.out.is_file():
        print(f"baseline missing: {args.out}")
        return 1
    if args.out.read_text(encoding="utf-8") != rendered:
        print(f"baseline DRIFT: {args.out} does not match a fresh read of {args.db}")
        return 1
    print(f"baseline matches {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``t3 eval merged-prs-since`` — the reusable scheduled-eval pre-check.

Reads the repo's merged PRs from a JSON file (a list of ``{number, merged_at}``
records — the platform query stays in CI YAML) and answers whether ANY PR merged
inside a lookback window. The shared core lives in
:mod:`teatree.eval.merged_prs`; the host's ``scripts/eval/merged_prs_since.py``
shim and this overlay-facing CLI both delegate to it. An overlay's weekly eval
workflow reuses this instead of duplicating the decision function.

Exit 0 → a PR merged in the window → run the eval. Exit ``--skip-code``
(default 1) → nothing merged → skip cleanly, no API spend. A non-list payload
exits 2. This is a PRE-CHECK that decides whether to invoke the eval at all — NOT
a skip-as-pass inside the eval (``--require-executed`` still fails the invoked
eval loud if it cannot execute).
"""

import json
from pathlib import Path

import typer

from teatree.eval.merged_prs import DEFAULT_DAYS, any_merged_since, parse_ts


def merged_prs_since(
    prs_file: Path = typer.Option(..., "--prs-file", help="JSON file: list of {number, merged_at} PR records."),
    days: int = typer.Option(DEFAULT_DAYS, "--days", help="Lookback window in days (default: 7)."),
    skip_code: int = typer.Option(1, "--skip-code", help="Exit code when the eval should be skipped."),
    now: str | None = typer.Option(None, "--now", help="Override 'now' (ISO-8601); for testing."),
) -> None:
    """Exit 0 if any PR merged in the last --days, else --skip-code (non-list payload exits 2)."""
    prs = json.loads(prs_file.read_text(encoding="utf-8"))
    if not isinstance(prs, list):
        typer.echo("--prs-file must contain a JSON list", err=True)
        raise SystemExit(2)
    if any_merged_since(prs, now=parse_ts(now) if now else None, days=days):
        typer.echo(f"a PR merged in the last {days} day(s) → run the weekly eval")
        return
    typer.echo(f"no PR merged in the last {days} day(s) — skipping, nothing new to test")
    raise SystemExit(skip_code)

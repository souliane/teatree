"""Record publish-safe clean passes in a local verdict ledger."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from teatree.eval.verdict_cache import VerdictLedger

if TYPE_CHECKING:
    from teatree.eval.triage import EvalSummaryRecord

verdict_cache_app = typer.Typer(help="Manage content-keyed eval pass verdicts.")


@verdict_cache_app.command("merge")
def merge(
    ledger: Path = typer.Argument(..., help="Ledger JSON to update."),
    sources: list[Path] = typer.Argument(..., help="Other verdict ledgers to combine."),
) -> None:
    cache = VerdictLedger.load(ledger)
    for source in sources:
        cache.merge(VerdictLedger.load(source))
    cache.write()
    typer.echo(f"verdict cache: {len(cache.entries)} clean passes in {ledger}")


@verdict_cache_app.command("record")
def record(
    ledger: Path = typer.Argument(..., help="Ledger JSON to update."),
    summaries: list[Path] | None = typer.Argument(None, help="Single-trial summary JSON files or directories."),
    run_url: str = typer.Option(..., "--run-url", help="Source run URL shown beside reused verdicts."),
) -> None:
    payloads: list[EvalSummaryRecord] = []
    for path in summaries or []:
        paths = sorted(path.glob("eval-*.json")) if path.is_dir() else [path]
        for summary in paths:
            try:
                payload = json.loads(summary.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                typer.echo(f"cannot read eval summary {summary}: {exc}", err=True)
                raise typer.Exit(code=2) from exc
            if not isinstance(payload, dict):
                typer.echo(f"eval summary {summary} must be a JSON object", err=True)
                raise typer.Exit(code=2)
            payloads.append(payload)
    cache = VerdictLedger.load(ledger)
    cache.record(payloads, run_url=run_url)
    cache.write()
    typer.echo(f"verdict cache: {len(cache.entries)} clean passes in {ledger}")

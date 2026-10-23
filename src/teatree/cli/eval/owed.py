"""``t3 eval owed`` — the owed-scenario ledger a lane writes and the nightly harvests and settles.

Thin shells over :mod:`teatree.eval.owed`. ``record`` and ``outputs`` run in the PR and nightly
eval jobs; ``collect``, ``next`` and ``settle`` are the nightly's harvest → run-oldest-first →
clear-on-pass cycle.
"""

import datetime as dt
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import NoReturn

import typer

from teatree.eval.artifact_redaction import write_artifact
from teatree.eval.discovery import discover_catalog
from teatree.eval.loader import load_eval_yaml
from teatree.eval.merged_prs import parse_ts
from teatree.eval.owed import OwedHarvest, OwedLedger, OwedLedgerError, OwedReason
from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.run import SUBPROCESS_UNREACHABLE, CommandFailedError, spawn_byte_pipe

owed_app = typer.Typer(
    help="The owed-scenario ledger: evals a lane could not run, re-run by the nightly until they pass."
)

_USAGE_EXIT = 2

_LEDGER_ARGUMENT = typer.Argument(..., help="The owed-ledger JSON file.")
_LEDGER_OUT_OPTION = typer.Option(..., "--out", help="The owed-ledger JSON file, updated in place.")
_NOW_OPTION = typer.Option(None, "--now", help="Override 'now' (ISO-8601); for testing.")
_SCENARIOS_DIR_OPTION = typer.Option(
    None,
    "--scenarios-dir",
    help="Read the catalog from this scenario directory (default: the discovered catalog).",
)


def _fail(message: str, code: int) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _moment(now: str | None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.UTC)
    try:
        return parse_ts(now)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--now") from exc


def _read(ledger: Path) -> OwedLedger:
    if not ledger.is_file():
        return OwedLedger()
    try:
        return OwedLedger.from_json(ledger.read_text(encoding="utf-8"))
    except OwedLedgerError as exc:
        _fail(f"MALFORMED OWED LEDGER {ledger}: {exc}", 1)


def _names(path: Path, option: str) -> list[str]:
    if not path.is_file():
        _fail(f"{option} {path} does not exist", _USAGE_EXIT)
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _catalog_names(scenarios_dir: Path | None, *, keep_when_degraded: Collection[str] = ()) -> frozenset[str]:
    if scenarios_dir is not None:
        names = frozenset(spec.name for path in sorted(scenarios_dir.glob("*.yaml")) for spec in load_eval_yaml(path))
        if not names:
            _fail(f"no eval scenarios under {scenarios_dir}: refusing to treat every owed one as gone", _USAGE_EXIT)
        return names
    ensure_django()
    catalog = discover_catalog()
    names = frozenset(spec.name for spec in catalog.specs)
    if catalog.is_complete:
        return names
    reasons = "; ".join(catalog.degraded.values())
    typer.echo(f"scenario catalog degraded ({reasons}): keeping every owed scenario it cannot see", err=True)
    return names | frozenset(keep_when_degraded)


def _note_unknown(names: Sequence[str]) -> None:
    if names:
        typer.echo(f"owed but not in the scenario catalog: {', '.join(names)}", err=True)


def _gh(args: Sequence[str]) -> bytes:
    command = ["gh", *args]
    pipe = spawn_byte_pipe(command)
    try:
        pipe.stdin.close()
        data = pipe.stdout.read()
    finally:
        pipe.stdout.close()
        code = pipe.process.wait()
    if code != 0:
        raise CommandFailedError(command, code, "", "")
    return data


@owed_app.command("record")
def record(
    ledger: Path = _LEDGER_OUT_OPTION,
    reason: OwedReason = typer.Option(..., "--reason", help="Why the lane could not run them."),
    source: str = typer.Option(..., "--source", help="Where the debt came from, e.g. pr#123@<sha> or nightly@<run>."),
    names_file: Path = typer.Option(..., "--names-file", help="Scenario names, one per line."),
    now: str | None = _NOW_OPTION,
) -> None:
    """Add the scenario names in --names-file to the --out ledger as owed."""
    names = _names(names_file, "--names-file")
    if not names:
        write_artifact(ledger, _read(ledger).to_json())
        typer.echo(f"nothing owed ({reason.value})")
        return
    owed = _read(ledger)
    updated = owed.recorded(names, reason=reason, source=source, since=_moment(now))
    write_artifact(ledger, updated.to_json())
    typer.echo(f"recorded {len(set(names))} owed scenario(s) ({reason.value})")


@owed_app.command("outputs")
def outputs(ledger: Path = _LEDGER_ARGUMENT) -> None:
    """Print the parked=/owed= lines a workflow step appends to $GITHUB_OUTPUT."""
    owed = _read(ledger)
    if any(name.splitlines() != [name] for name in owed.names):
        _fail("owed scenario name must fit on one output line", 1)
    typer.echo(f"parked={'true' if owed.is_parked else 'false'}")
    typer.echo(f"owed={','.join(owed.names)}")


@owed_app.command("collect")
def collect(
    repo: str = typer.Option(..., "--repo", help="OWNER/NAME of the repository whose artifacts hold the debt."),
    out: Path = typer.Option(..., "--out", help="Where to write the harvested ledger."),
    now: str | None = _NOW_OPTION,
    first_lookback_days: int = typer.Option(1, "--first-lookback-days", min=1),
) -> None:
    """Fold the previous nightly ledger with the owed artifacts of merged PRs and weekly and heal legs."""
    harvest = OwedHarvest(
        gh=_gh,
        repo=repo,
        warn=typer.echo,
        first_harvest_lookback=dt.timedelta(days=first_lookback_days),
    )
    try:
        ledger = harvest.collect(_moment(now))
    except (OwedLedgerError, CommandFailedError, *SUBPROCESS_UNREACHABLE) as exc:
        _fail(f"eval owed collect failed: {exc}", 1)
    write_artifact(out, ledger.to_json())
    harvested_through = ledger.harvested_through.isoformat() if ledger.harvested_through else "never"
    typer.echo(f"collected {len(ledger.scenarios)} owed scenario(s), harvested through {harvested_through}")


@owed_app.command("next")
def next_batch(
    ledger: Path = _LEDGER_ARGUMENT,
    cap: int = typer.Option(..., "--cap", min=1, help="The most owed scenarios to run."),
    scenarios_dir: Path | None = _SCENARIOS_DIR_OPTION,
) -> None:
    """Print the oldest owed scenarios the catalog still defines, one per line."""
    if not ledger.is_file():
        return
    owed = _read(ledger)
    known = _catalog_names(scenarios_dir)
    _note_unknown([name for name in owed.names if name not in known])
    for name in owed.next_batch(cap, known):
        typer.echo(name)


@owed_app.command("settle")
def settle(
    ledger: Path = _LEDGER_OUT_OPTION,
    passed_file: Path = typer.Option(..., "--passed-file", help="Scenario names that passed, one per line."),
    attempted_file: Path | None = typer.Option(None, "--attempted-file", help="Scenario names attempted tonight."),
    scenarios_dir: Path | None = _SCENARIOS_DIR_OPTION,
) -> None:
    """Clear the owed scenarios that passed and the ones the catalog no longer defines."""
    if not ledger.is_file():
        _fail(f"no owed ledger at {ledger}: run `t3 eval owed collect` first", _USAGE_EXIT)
    owed = _read(ledger)
    passed = _names(passed_file, "--passed-file")
    attempted = _names(attempted_file, "--attempted-file") if attempted_file is not None else []
    settled, dropped = owed.settled(
        passed, _catalog_names(scenarios_dir, keep_when_degraded=owed.names), attempted=attempted
    )
    _note_unknown(dropped)
    write_artifact(ledger, settled.to_json())
    cleared = len(owed.scenarios) - len(settled.scenarios) - len(dropped)
    typer.echo(f"settled {cleared} passed and dropped {len(dropped)} unknown; {len(settled.scenarios)} still owed")

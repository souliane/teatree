"""``t3 eval set-baseline`` — derive the file-backed ``baseline`` preset from a matrix run.

Reads a ``t3 eval run --models ... --format json`` (or ``t3 eval benchmark
--format json``) matrix payload and writes ``evals/presets/baseline.yaml``: for
each currently-discovered scenario, the CHEAPEST tier (cheap < balanced <
frontier) whose matrix cell passed. A scenario that failed at every tier gets
NO entry (a warning, never a guess) and a scenario no longer discovered is
pruned. The whole file is regenerated from the matrix — never merged with the
prior contents — so the output is always exactly what the input run proves.
"""

from pathlib import Path

import typer
import yaml

from teatree.agents.model_tiering import TIER_MODELS
from teatree.core.cost import tier_rank
from teatree.eval.discovery import discover_specs
from teatree.eval.matrix_payload import MatrixPayloadError, load_matrix_payload
from teatree.eval.presets import BASELINE_PRESET_PATH, PresetError
from teatree.utils.django_bootstrap import ensure_django

#: model id -> abstract tier name, the reverse of TIER_MODELS — a matrix column
#: not one of these three shipped ids cannot be mapped back to a tier at all.
_TIER_BY_MODEL: dict[str, str] = {model: tier for tier, model in TIER_MODELS.items()}

_FRONTIER_TIER = "frontier"


def set_baseline(
    from_matrix: Path = typer.Option(
        ...,
        "--from",
        exists=True,
        readable=True,
        help=(
            "Matrix JSON to derive the baseline from — the output of "
            "`t3 eval run --models <tier models> --format json` (or `t3 eval benchmark --format json`)."
        ),
    ),
    allow_frontier: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--allow-frontier",
        help=(
            "Permit assigning the frontier tier to a scenario that only passed there. "
            "Without this, such a scenario aborts the write (exit 2) rather than silently "
            "pinning the most expensive tier. When passed, the scenario is ALSO recorded "
            "under frontier_ok in the same file."
        ),
    ),
    out: Path = typer.Option(
        BASELINE_PRESET_PATH,
        "--out",
        help="Baseline file to write (default: evals/presets/baseline.yaml).",
    ),
) -> None:
    """Regenerate the ``baseline`` preset file from a model-matrix JSON run.

    For each scenario in *from_matrix* that is still discovered, picks the
    cheapest tier whose cell passed (not skipped, not errored). A scenario
    failing every tier is skipped with a warning — never guessed. A scenario in
    the matrix that is no longer discovered (renamed/removed) is pruned. Output
    is deterministic: scenario keys sorted, ``frontier_ok`` sorted.
    """
    ensure_django()
    try:
        payload = load_matrix_payload(from_matrix)
    except MatrixPayloadError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    discovered = {spec.name for spec in discover_specs()}
    scenario_tiers: dict[str, str] = {}
    frontier_ok: set[str] = set()
    unresolved: list[str] = []
    for entry in payload.scenarios:
        if entry.name not in discovered:
            continue
        try:
            tier = _cheapest_passing_tier(entry.results)
        except PresetError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from None
        if tier is None:
            unresolved.append(entry.name)
            continue
        if tier == _FRONTIER_TIER:
            if not allow_frontier:
                typer.echo(
                    f"scenario {entry.name!r} only passed at the frontier tier; pass --allow-frontier to "
                    "record it (it will also be listed under frontier_ok).",
                    err=True,
                )
                raise typer.Exit(code=2)
            frontier_ok.add(entry.name)
        scenario_tiers[entry.name] = tier
    for name in sorted(unresolved):
        typer.echo(f"WARNING {name}: failed at every tier in the matrix — no baseline entry written", err=True)
    _write_baseline(out, scenario_tiers, frontier_ok)
    typer.echo(f"wrote {len(scenario_tiers)} scenario tier(s) to {out}")


def _cheapest_passing_tier(results: dict[str, dict[str, object] | None]) -> str | None:
    """The cheapest tier whose cell PASSED, or ``None`` if nothing passed."""
    passing_models = [
        model
        for model, cell in results.items()
        if isinstance(cell, dict) and cell.get("passed") is True and not cell.get("skipped") and not cell.get("errored")
    ]
    unknown = [model for model in passing_models if model not in _TIER_BY_MODEL]
    if unknown:
        msg = (
            f"matrix column {unknown[0]!r} is not one of the shipped TIER_MODELS values "
            f"({sorted(TIER_MODELS.values())}); set-baseline requires a matrix run against the tier models."
        )
        raise PresetError(msg)
    known = [model for model in passing_models if model in _TIER_BY_MODEL]
    if not known:
        return None
    return _TIER_BY_MODEL[min(known, key=tier_rank)]


def _write_baseline(path: Path, scenario_tiers: dict[str, str], frontier_ok: set[str]) -> None:
    payload = {
        "scenarios": dict(sorted(scenario_tiers.items())),
        "frontier_ok": sorted(frontier_ok),
    }
    header = "# GENERATED by t3 eval set-baseline\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + yaml.safe_dump(payload, sort_keys=False, default_flow_style=False), encoding="utf-8")

"""``t3 eval set-baseline`` — derive the file-backed ``baseline`` preset from a matrix run.

Reads a matrix payload and writes ``evals/presets/baseline.yaml``: for each
currently-discovered scenario, the CHEAPEST tier (cheap < balanced < frontier)
whose matrix cell passed. A scenario that failed at every tier gets NO entry (a
warning, never a guess) and a scenario no longer discovered is pruned. The whole
file is regenerated from the matrix — never merged with the prior contents — so
the output is always exactly what the input run proves.

The input matrix comes from either producer, both emitting the same JSON shape:

*   ``t3 eval ladder --format json`` — the CHEAP producer. Escalates each
    scenario cheapest-first and measures opus only on the scenarios both haiku
    and sonnet failed, so the derivation is loss-free while paying a fraction of
    the full matrix. A never-reached tier is an ABSENT (null) cell, which the
    cheapest-passing derivation below already treats as "not passing there".
*   ``t3 eval run --models <tiers> --format json`` / ``t3 eval benchmark
    --format json`` — the FULL matrix (every model on every scenario), for when
    every cell should be measured.

A matrix produced BEFORE a tier bump names a model the shipped
:data:`~teatree.agents.model_tiering.TIER_MODELS` no longer contains. Such a
column is dropped with a loud warning rather than aborting the regeneration: it
is evidence about a model nothing resolves to today, and dropping a candidate
from the cheapest-passing choice can only raise the derived tier or leave the
scenario unpinned — never cheapen it below a measured pass. The one case that
still refuses is a matrix in which EVERY passing column is stale, where writing
the derivation would silently replace the existing pins with an empty map.
"""

from collections import Counter
from pathlib import Path

import typer
import yaml

from teatree.agents.model_tiering import TIER_MODELS
from teatree.core.cost import tier_rank
from teatree.eval.discovery import discover_specs
from teatree.eval.matrix_payload import MatrixCell, MatrixPayloadError, load_matrix_payload
from teatree.eval.presets import BASELINE_HEADER, BASELINE_PRESET_PATH
from teatree.utils.django_bootstrap import ensure_django

#: model id -> abstract tier name, the reverse of TIER_MODELS — a matrix column
#: not one of these three shipped ids cannot be mapped back to a tier at all.
_TIER_BY_MODEL: dict[str, str] = {model: tier for tier, model in TIER_MODELS.items()}

_FRONTIER_TIER = "frontier"

_REGENERATE_HINT = (
    "Regenerate the matrix against the current tier models: "
    "`t3 eval ladder --format json > matrix.json`, then re-run `t3 eval set-baseline --from matrix.json`."
)


def set_baseline(
    from_matrix: Path = typer.Option(
        ...,
        "--from",
        exists=True,
        readable=True,
        help=(
            "Matrix JSON to derive the baseline from — the output of `t3 eval ladder --format json` "
            "(the cheap escalation-ladder producer) or a full matrix from "
            "`t3 eval run --models <tier models> --format json` / `t3 eval benchmark --format json`."
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
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Baseline file to write (default: evals/presets/baseline.yaml).",
        show_default=False,
    ),
) -> None:
    """Regenerate the ``baseline`` preset file from a model-matrix JSON run.

    For each scenario in *from_matrix* that is still discovered, picks the
    cheapest tier whose cell passed (not skipped, not errored). A scenario
    failing every tier is skipped with a warning — never guessed. A scenario in
    the matrix that is no longer discovered (renamed/removed) is pruned. A
    column naming a model outside the shipped tier models is warned about and
    dropped, so a matrix from before a tier bump still yields the pins its
    current columns prove. Output is deterministic: scenario keys sorted,
    ``frontier_ok`` sorted.
    """
    ensure_django()
    # Resolved here, not as the Option default: an absolute __file__-derived
    # default renders per-environment and breaks the docs-drift gate.
    target = out if out is not None else BASELINE_PRESET_PATH
    try:
        payload = load_matrix_payload(from_matrix)
    except MatrixPayloadError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    discovered = {spec.name for spec in discover_specs()}
    scenario_tiers: dict[str, str] = {}
    frontier_ok: set[str] = set()
    unresolved: list[str] = []
    stale_columns: Counter[str] = Counter()
    for entry in payload.scenarios:
        if entry.name not in discovered:
            continue
        passing = _passing_models(entry.results)
        stale_columns.update(model for model in passing if model not in _TIER_BY_MODEL)
        tier = _cheapest_passing_tier(passing)
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
    for column, affected in sorted(stale_columns.items()):
        typer.echo(
            f"WARNING matrix column {column!r} is not one of the shipped tier models "
            f"({sorted(TIER_MODELS.values())}) — its passing cells are ignored on {affected} scenario(s). "
            f"{_REGENERATE_HINT}",
            err=True,
        )
    for name in sorted(unresolved):
        typer.echo(f"WARNING {name}: failed at every tier in the matrix — no baseline entry written", err=True)
    if stale_columns and not scenario_tiers:
        typer.echo(
            f"no scenario resolved a tier: every passing matrix column is stale ({sorted(stale_columns)}). "
            f"Refusing to overwrite {target} with an empty map. {_REGENERATE_HINT}",
            err=True,
        )
        raise typer.Exit(code=2)
    _write_baseline(target, scenario_tiers, frontier_ok)
    typer.echo(f"wrote {len(scenario_tiers)} scenario tier(s) to {target}")


def _passing_models(results: dict[str, MatrixCell | None]) -> list[str]:
    """The model columns whose cell is a genuine PASS (not absent/skipped/errored)."""
    return [
        model
        for model, cell in results.items()
        if cell is not None and cell.passed and not cell.skipped and not cell.errored
    ]


def _cheapest_passing_tier(passing_models: list[str]) -> str | None:
    """The cheapest tier whose cell PASSED, or ``None`` if no CURRENT tier model passed.

    A column outside :data:`_TIER_BY_MODEL` is dropped: it carries no evidence
    about any tier model shipped today. Dropping a candidate can only raise the
    derived tier or remove the pin, never cheapen it below a measured pass.
    """
    known = [model for model in passing_models if model in _TIER_BY_MODEL]
    if not known:
        return None
    return _TIER_BY_MODEL[min(known, key=_model_tier_rank)]


def _model_tier_rank(model: str) -> int:
    """``tier_rank`` narrowed to a required model id, so ``min`` still returns ``str``."""
    return tier_rank(model)


def _write_baseline(path: Path, scenario_tiers: dict[str, str], frontier_ok: set[str]) -> None:
    payload = {
        "scenarios": dict(sorted(scenario_tiers.items())),
        "frontier_ok": sorted(frontier_ok),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    path.write_text(f"{BASELINE_HEADER}\n{body}", encoding="utf-8")

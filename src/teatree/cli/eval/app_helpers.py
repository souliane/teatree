"""Small argument-validation helpers for the ``t3 eval run`` command body.

Held apart from :mod:`teatree.cli.eval.app` (which is at its module-LOC cap) so
the command body stays thin: each resolves one CLI argument to its validated
domain value, or exits 2 (usage error) naming the valid choices.
"""

import dataclasses
from pathlib import Path
from typing import cast

import typer
from claude_agent_sdk.types import EffortLevel

from teatree.agents.model_tiering import resolve_tier
from teatree.cli.eval.escalate import EscalationConfig
from teatree.eval.backends import API_BACKEND
from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.model_variant import EFFORT_LEVELS
from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult, render_html, render_summary_markdown
from teatree.eval.summary_json import write_summary_json

#: The tiers the ``--benchmark`` matrix compares, strongest → cheapest, so the
#: matrix columns read in capability order. Resolved to concrete models through
#: the single TIER_MODELS constant at run time — so the benchmark adopts a new
#: model the instant TIER_MODELS (or [agent.tier_models]) changes, no flag edit.
BENCHMARK_TIERS: tuple[str, ...] = ("frontier", "balanced", "cheap")


def benchmark_models() -> str:
    """The comma-joined ``--models`` list for ``--benchmark``: each tier's model.

    Resolves :data:`BENCHMARK_TIERS` through :func:`resolve_tier` so the benchmark
    runs every scenario against the concrete model behind each of the three
    tiers. The single source of truth (``TIER_MODELS`` / ``[agent.tier_models]``)
    decides the models — adopting a new one needs no benchmark-flag edit.
    """
    return ",".join(resolve_tier(tier) for tier in BENCHMARK_TIERS)


@dataclasses.dataclass(frozen=True)
class BenchmarkSelection:
    """The model-lane selection resolved from ``--benchmark`` / ``--model`` / ``--models``.

    Exactly one of the three (or none) may be active. ``models`` is the resolved
    comma-list for the matrix lane (the benchmark expands to the three tier
    models); ``model_override`` forces the whole suite onto one model;
    ``benchmark_html`` is the HTML artifact path the benchmark renders.
    """

    models: str | None
    model_override: str | None
    benchmark_html: Path | None


def resolve_benchmark_selection(
    *, benchmark: bool, model: str | None, models: str | None, html_out: Path | None
) -> BenchmarkSelection:
    """Validate ``--benchmark``/``--model``/``--models`` are mutually exclusive, then resolve.

    ``--benchmark`` expands to the three tier models (the matrix lane) and renders
    the HTML dashboard at *html_out*. ``--model`` forces the whole suite onto one
    model. ``--models`` is the explicit matrix list (unchanged). At most one may
    be set; combining any two is a usage error (exit 2).
    """
    active = [
        name
        for name, on in (("--benchmark", benchmark), ("--model", model is not None), ("--models", models is not None))
        if on
    ]
    if len(active) > 1:
        typer.echo(f"{' and '.join(active)} are mutually exclusive; pass at most one.", err=True)
        raise typer.Exit(code=2)
    if benchmark:
        return BenchmarkSelection(models=benchmark_models(), model_override=None, benchmark_html=html_out)
    return BenchmarkSelection(models=models, model_override=model, benchmark_html=None)


def require_spec(name: str) -> EvalSpec:
    """Resolve a scenario by *name*, or exit 2 listing the available scenarios."""
    spec = find_spec(name)
    if spec is None:
        typer.echo(f"unknown scenario: {name!r}", err=True)
        available = ", ".join(s.name for s in discover_specs()) or "(none)"
        typer.echo(f"available scenarios: {available}", err=True)
        raise typer.Exit(code=2)
    return spec


def require_effort(effort: str) -> EffortLevel:
    """Validate ``--effort`` against the known levels, or exit 2 listing them."""
    if effort not in EFFORT_LEVELS:
        typer.echo(f"unknown --effort {effort!r}; known levels: {', '.join(EFFORT_LEVELS)}", err=True)
        raise typer.Exit(code=2)
    return cast("EffortLevel", effort)


def require_api_backend_for_fresh_run(*, backend: str, trials: int, models: str | None) -> None:
    """Reject fresh-run-only shapes unless the caller explicitly opts into api."""
    if trials == 1 and models is None:
        return
    if backend == API_BACKEND:
        return
    typer.echo(
        f"--trials/--models require a fresh metered run; pass --backend api instead of --backend {backend!r}.",
        err=True,
    )
    raise typer.Exit(code=2)


def resolve_escalation(
    *, escalate_on_fail: bool, escalate_trials: int, trials: int, models: str | None
) -> EscalationConfig | None:
    """Validate ``--escalate-on-fail`` and return its config, or ``None`` when off.

    Escalation only makes sense on the single-trial lane (``--trials 1``, no
    ``--models``): the multi-trial and matrix shapes already aggregate across
    trials, so re-running their failures would double-count. A request to escalate
    a multi-trial/matrix run is a usage error (exit 2). ``escalate_trials`` must be
    ``>= 2`` — a single escalation trial is no escalation.
    """
    if not escalate_on_fail:
        return None
    if trials > 1 or models is not None:
        typer.echo(
            "--escalate-on-fail applies to the single-trial lane only; --trials>1 and --models "
            "already aggregate across trials. Drop --escalate-on-fail or drop --trials/--models.",
            err=True,
        )
        raise typer.Exit(code=2)
    if escalate_trials < 2:  # noqa: PLR2004 — one trial is no escalation; the single trial already ran it.
        typer.echo(
            f"--escalate-trials must be >= 2 (got {escalate_trials}); a single trial is no escalation.", err=True
        )
        raise typer.Exit(code=2)
    return EscalationConfig(escalate_trials=escalate_trials)


@dataclasses.dataclass(frozen=True)
class RunReportPaths:
    """The per-run report artifact output paths — each ``None`` when not requested.

    ``transcript_html`` is the private per-trial transcript; ``summary_md`` and
    ``summary_json`` are the sanitized, publish-safe dashboards. Grouped so the
    matrix-shape guard iterates the report flags rather than repeating one
    near-identical rejection per flag.
    """

    transcript_html: Path | None = None
    summary_md: Path | None = None
    summary_json: Path | None = None

    def matrix_incompatible(self) -> tuple[tuple[Path | None, str, str], ...]:
        """The report flags a ``--models`` matrix cannot render, with their message parts."""
        return (
            (self.transcript_html, "--transcript-html", "the per-TRIAL transcript report (a --trials run)"),
            (self.summary_md, "--summary-md", "the single-trial / --trials aggregate dashboard"),
            (self.summary_json, "--summary-json", "the single-trial / --trials per-scenario artifact"),
        )


def reject_unsupported_run_output(
    *, output_format: str, reports: RunReportPaths, trials: int, models: str | None
) -> None:
    """Reject the report flags on the multi-trial/matrix shapes they don't support.

    ``--format html`` renders a SINGLE-trial ``list[ScenarioResult]`` and the
    ``reports`` flags render the single-trial or per-TRIAL results; a ``--models``
    matrix has neither, so each is a usage error there (and ``--format html`` is
    likewise rejected for ``--trials``). Exits 2 naming the fix rather than failing
    obscurely deeper in the run.
    """
    if output_format == "html" and (trials > 1 or models is not None):
        typer.echo("--format html is only supported for a single-trial run (not --trials/--models)", err=True)
        raise typer.Exit(code=2)
    if models is None:
        return
    for value, flag, description in reports.matrix_incompatible():
        if value is not None:
            typer.echo(
                f"{flag} is {description}; a --models matrix has none to render. Drop --models or drop {flag}.",
                err=True,
            )
            raise typer.Exit(code=2)


def write_single_trial_reports(
    results: list[ScenarioResult],
    *,
    transcript_html: Path | None,
    summary_md: Path | None,
    summary_json: Path | None = None,
) -> None:
    """Write the single-trial transcript HTML, sanitized summary markdown, and/or JSON.

    All are written from THIS run's results (no re-run) and BEFORE any guard/gate
    can exit, so a red run still drops each artifact. ``transcript_html`` is the
    full per-scenario transcript (private); ``summary_md`` / ``summary_json`` are
    the sanitized, publish-safe dashboards (no transcript). Each path being
    ``None`` is a no-op.
    """
    if transcript_html is not None:
        transcript_html.write_text(render_html(results), encoding="utf-8")
    if summary_md is not None:
        summary_md.write_text(render_summary_markdown(results), encoding="utf-8")
    if summary_json is not None:
        write_summary_json(results, summary_json)

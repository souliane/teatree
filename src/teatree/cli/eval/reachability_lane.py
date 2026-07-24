"""``t3 eval reachability`` — scenario/fixture command-reachability lane (#3566, F12).

The thin CLI surface over the pure :mod:`teatree.eval.scenario_reachability`
engine. It walks every ``t3 …`` invocation a shipped scenario YAML or a ``_pass``
/``_fail`` transcript fixture cites against the LIVE CLI registry, so a scenario
whose expectation names a nonexistent ``t3`` command surfaces at authoring time
instead of silently grading a path the product cannot take.

**Dependency inversion.** The live ``(valid_paths, group_paths)`` registry is
built by :func:`teatree.cli.eval.skill_command_lane.build_command_registry` — the
same registered provider ``t3 eval skill-command-validity`` consumes — so this
lane never reaches UP into ``teatree.cli`` (the backwards edge tach forbids).

**Advisory by default.** The shipped corpus still carries known false-positive
references dominated by two precision gaps (overlay-slot fixture names, prose
fragments), so the DEFAULT lane REPORTS and exits 0 — it never reds a PR.
``--fail-on-unreachable`` opts into gating once those gaps close, mirroring
``t3 eval coverage --fail-on-gap``.
"""

import sys

import typer

from teatree.cli._format_opts import require_valid_format
from teatree.cli.eval.skill_command_lane import build_command_registry
from teatree.eval.scenario_reachability import (
    DEFAULT_FIXTURES_DIR,
    DEFAULT_SCENARIOS_DIR,
    ReachabilityReport,
    validate_scenario_reachability,
)
from teatree.utils.django_bootstrap import ensure_django


def validate_shipped_scenario_reachability() -> ReachabilityReport:
    """Check every shipped scenario/fixture ``t3 …`` against the live registry (the lane body)."""
    valid, groups = build_command_registry()
    return validate_scenario_reachability(
        valid, groups, scenarios_dir=DEFAULT_SCENARIOS_DIR, fixtures_dir=DEFAULT_FIXTURES_DIR
    )


def reachability(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
    fail_on_unreachable: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--fail-on-unreachable",
        help="Exit non-zero on any unreachable reference; default is advisory (report + exit 0).",
    ),
) -> None:
    """Report scenario/fixture ``t3 …`` invocations that name no live CLI command.

    Tier-1 (deterministic, free, no ``claude`` run): each scenario YAML and each
    ``_pass``/``_fail`` transcript fixture is scanned for ``t3 …`` runs, which are
    token-walked against the live typer command tree. A reference that resolves to
    nothing grades a path the product cannot take. ADVISORY by default (the shipped
    corpus carries known false positives from two precision gaps — overlay-slot
    fixture names and prose fragments); ``--fail-on-unreachable`` flips it to a gate.
    """
    ensure_django()
    require_valid_format(output_format)
    report = validate_shipped_scenario_reachability()
    if output_format == "json":
        import json  # noqa: PLC0415 — deferred: loaded only when this command runs

        typer.echo(
            json.dumps(
                {
                    "ok": report.ok,
                    "checked": report.checked,
                    "unreachable": [{"source": u.source, "command": u.command} for u in report.unreachable],
                },
                indent=2,
            )
        )
    else:
        typer.echo(report.render_text())
    if fail_on_unreachable and not report.ok:
        sys.exit(1)

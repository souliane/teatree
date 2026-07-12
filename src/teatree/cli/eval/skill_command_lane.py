"""``t3 eval skill-command-validity`` — Tier-1 command-validity lane (#550, free).

The thin CLI surface over the pure :mod:`teatree.eval.skill_command_validity`
engine. The engine validates SKILL.md ``t3 …`` invocations against an injected
``(valid_paths, group_paths)`` registry built from the LIVE typer command tree
(``teatree.cli_reference.command_paths`` / ``command_groups`` — the SSOT for "is
``t3 <sub> …`` a real command"), so a renamed/removed subcommand cited in a skill
doc FAILs the lane.

**Dependency inversion.** Building that registry needs the assembled root ``t3``
app, which only ``teatree.cli`` (the parent of ``teatree.cli.eval``) can hand
over — a direct import here would be a backwards edge (``teatree.cli`` already
depends on ``teatree.cli.eval``, so it would be a cycle tach forbids). The lane
therefore consumes a REGISTERED provider: ``teatree.cli`` calls
:func:`register_command_registry_provider` at import time to inject a closure
that builds the registry from its own app. The default provider raises a clear
error, so a caller that never registered one fails loud rather than silently
validating against an empty registry.

Free and deterministic — no model, no spend. Wired into ``t3 eval`` (the
free-lane summary) and exposed as the standalone ``t3 eval skill-command-validity``.
"""

import sys
from collections.abc import Callable
from pathlib import Path

import typer

from teatree.cli._format_opts import require_valid_format
from teatree.cli.eval.verdict import LaneResult
from teatree.eval.skill_command_validity import DEFAULT_SKILLS_DIR, CommandValidityReport, validate_skill_commands
from teatree.utils.django_bootstrap import ensure_django

#: Builds the live ``(valid_paths, group_paths)`` registry. Registered by
#: ``teatree.cli`` (which owns the assembled root app); inverted to break the
#: ``teatree.cli.eval → teatree.cli`` backwards edge.
RegistryProvider = Callable[[], tuple[set[str], set[str]]]


def _unregistered_provider() -> tuple[set[str], set[str]]:
    msg = (
        "skill-command-validity registry provider not registered — "
        "teatree.cli must call register_command_registry_provider() at import time"
    )
    raise RuntimeError(msg)


_registry_provider: RegistryProvider = _unregistered_provider


def register_command_registry_provider(provider: RegistryProvider) -> None:
    """Inject the registry builder (called by ``teatree.cli`` at import time)."""
    global _registry_provider  # noqa: PLW0603 — the single registration seam for the inverted dependency
    _registry_provider = provider


def build_command_registry() -> tuple[set[str], set[str]]:
    """The live ``(valid_paths, group_paths)`` via the registered provider."""
    return _registry_provider()


def validate_shipped_skill_commands(skills_dir: Path = DEFAULT_SKILLS_DIR) -> CommandValidityReport:
    """Validate the shipped skill docs against the live registry (the lane body)."""
    valid, groups = build_command_registry()
    return validate_skill_commands(valid, groups, skills_dir=skills_dir)


def skill_command_validity_lane(report: CommandValidityReport) -> LaneResult:
    """Fold a validity report into the free ``skill-command-validity`` lane for ``t3 eval``."""
    detail = (
        f"{report.checked} `t3 …` invocation(s) all resolve"
        if report.ok
        else f"{len(report.violations)} stale `t3 …` reference(s) of {report.checked} checked"
    )
    return LaneResult(
        name="skill-command-validity",
        cost="free",
        passed=report.ok,
        skipped=False,
        detail=detail,
    )


def skill_command_validity(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """Validate every backticked ``t3 …`` in the skill docs against the live CLI registry.

    Tier-1 (deterministic, free, no ``claude`` run): each ``skills/<name>/`` doc's
    backticked ``t3 …`` commands are token-walked against the live typer command
    tree. A command that no longer resolves (a CLI rename left the doc stale) is a
    violation — the "no stale references" rule — and exits non-zero. Generic
    placeholder mentions (``t3 …`` / ``t3 <overlay> …``) are skipped.
    """
    ensure_django()
    require_valid_format(output_format)
    report = validate_shipped_skill_commands()
    if output_format == "json":
        import json  # noqa: PLC0415 — deferred: loaded only when this command runs

        typer.echo(
            json.dumps(
                {
                    "ok": report.ok,
                    "checked": report.checked,
                    "violations": [{"skill": v.skill, "doc": v.doc, "command": v.command} for v in report.violations],
                },
                indent=2,
            )
        )
    else:
        typer.echo(report.render_text())
    if not report.ok:
        sys.exit(1)

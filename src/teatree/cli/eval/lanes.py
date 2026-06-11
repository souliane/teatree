"""Deterministic single-lane ``t3 eval`` subcommands (free — no ``claude`` run).

Held apart from the ``run`` body and the bare-suite callback in
:mod:`teatree.cli.eval.app`: each is a self-contained free lane that renders one
report and exits non-zero on a violation, sharing none of the runner/persist/gate
machinery. Registered onto ``eval_app`` from ``app`` via the same
``command(name)(func)`` indirection the other split-out lanes use.
"""

import sys

import typer

from teatree.cli._format_opts import require_valid_format
from teatree.eval.coverage import render_json as render_coverage_json
from teatree.eval.coverage import render_text as render_coverage_text
from teatree.eval.coverage import skill_eval_coverage
from teatree.eval.regression_corpus import render_json as render_regression_json
from teatree.eval.regression_corpus import render_text as render_regression_text
from teatree.eval.regression_corpus import run_regression_corpus
from teatree.eval.trigger_qa import render_json as render_trigger_json
from teatree.eval.trigger_qa import render_text as render_trigger_text
from teatree.eval.trigger_qa import run_trigger_qa
from teatree.utils.django_bootstrap import ensure_django


def skill_triggers(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """Validate every skill's trigger keywords against the must-fire/must-not-fire corpus.

    Deterministic and free — no ``claude -p`` invocation. An under-trigger
    (in-scope prompt that does not fire) or over-trigger (control prompt that
    does fire) exits non-zero.
    """
    report = run_trigger_qa()
    typer.echo(render_trigger_json(report) if output_format == "json" else render_trigger_text(report))
    if not report.ok:
        sys.exit(1)


def coverage(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
    fail_on_gap: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--fail-on-gap",
        help="Exit non-zero on any coverage gap (Phase B enforcement); default is warn-first (exit 0).",
    ),
) -> None:
    """Report per-skill behavioral-eval coverage: every skill is covered or eval_exempt.

    A skill is COVERED when >=1 discovered scenario targets its ``SKILL.md``
    (flat catalog OR co-located ``skills/<name>/evals.yaml``), or EXEMPT when its
    frontmatter carries a non-empty ``eval_exempt`` reason. A skill that is
    neither is a GAP. Deterministic and free — no ``claude -p`` invocation.
    Warn-first by default (a gap is reported, exit 0); ``--fail-on-gap`` is the
    Phase-B enforcement that exits non-zero on any gap.
    """
    ensure_django()
    require_valid_format(output_format)
    report = skill_eval_coverage()
    typer.echo(render_coverage_json(report) if output_format == "json" else render_coverage_text(report))
    if fail_on_gap and report.gaps:
        sys.exit(1)


def pinned_regressions(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """Run the deterministic regression corpus over the real gate/checker code paths.

    Layer-1 (deterministic, free, no ``claude`` run): each check calls the real
    function for a recurring failure class (branch-currency §940, the
    bare-reference gate, the substrate-merge and maker≠checker floors, the
    pid-anchored loop lease, the migration-graph leaf count) on a must-block and
    a must-allow input. Any violated invariant exits non-zero.
    """
    ensure_django()
    require_valid_format(output_format)
    report = run_regression_corpus()
    typer.echo(render_regression_json(report) if output_format == "json" else render_regression_text(report))
    if not report.ok:
        sys.exit(1)

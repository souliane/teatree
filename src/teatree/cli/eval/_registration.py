"""Wire the pre-built ``t3 eval`` subcommands onto the eval Typer app.

The eval app's commands fall into two concerns: the inline command DEFINITIONS
(``list`` / ``run`` / the bare-``t3 eval`` callback, defined with their full
option lists in :mod:`teatree.cli.eval.app`) and the WIRING of subcommands that
are already-built callables / sub-Typers living in their own modules. This module
owns that wiring concern: it imports each pre-built command and mounts it on the
passed ``eval_app``. Keeping the registration list here leaves ``app.py`` to the
command definitions alone (module-health split-by-concern).
"""

import typer

from teatree.cli.eval.audit import audit
from teatree.cli.eval.benchmark import benchmark
from teatree.cli.eval.capture_subagent import capture_subagent
from teatree.cli.eval.changed_scenarios import changed_scenarios
from teatree.cli.eval.ci_heal import ci_heal_app
from teatree.cli.eval.ci_status import ci_status
from teatree.cli.eval.ci_trigger import ci_trigger
from teatree.cli.eval.corpus import corpus_app
from teatree.cli.eval.green_proof import green_proof
from teatree.cli.eval.history import history_command
from teatree.cli.eval.label import label_app
from teatree.cli.eval.lanes import coverage, pinned_regressions
from teatree.cli.eval.merge_summaries import merge_summaries
from teatree.cli.eval.merge_summary_json import merge_summary_json
from teatree.cli.eval.merged_prs_since import merged_prs_since
from teatree.cli.eval.negative_control import negative_control
from teatree.cli.eval.prepare_transcript import prepare_transcript
from teatree.cli.eval.skill_command_lane import skill_command_validity
from teatree.cli.eval.skill_prose_lane import skill_prose_judge
from teatree.cli.eval.transcript_replay import transcript_replay


def register_imported_commands(eval_app: typer.Typer) -> None:
    """Mount every pre-built subcommand and sub-Typer onto *eval_app*."""
    eval_app.command("negative-control")(negative_control)
    eval_app.command("benchmark")(benchmark)
    eval_app.command("capture-subagent")(capture_subagent)
    eval_app.command("transcript-replay")(transcript_replay)
    eval_app.command("coverage")(coverage)
    eval_app.command("pinned-regressions")(pinned_regressions)
    eval_app.command("skill-command-validity")(skill_command_validity)
    eval_app.command("skill-prose-judge")(skill_prose_judge)
    eval_app.command("audit")(audit)
    eval_app.command("changed-scenarios")(changed_scenarios)
    eval_app.command("ci-trigger")(ci_trigger)
    eval_app.command("ci-status")(ci_status)
    eval_app.add_typer(ci_heal_app, name="ci-heal")
    eval_app.command("green-proof")(green_proof)
    eval_app.command("merged-prs-since")(merged_prs_since)
    eval_app.command("merge-summaries")(merge_summaries)
    eval_app.command("merge-summary-json")(merge_summary_json)
    eval_app.command("prepare-transcript")(prepare_transcript)
    eval_app.command("history")(history_command)
    eval_app.add_typer(corpus_app, name="corpus")
    eval_app.add_typer(label_app, name="label")

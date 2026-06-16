"""``t3 eval corpus`` — list, inspect, and grade the ground-truth corpus (#2192).

Thin readers over the committed corpus engine — the modules own the behaviour
(:mod:`teatree.eval.corpus_loader` discovers and validates labels,
:mod:`teatree.eval.corpus_grade` grades a captured session through the same
``report.evaluate`` path a scenario uses); the commands here only coordinate.
``grade`` is free and deterministic under the ``--no-judge`` default: a
judge-oracle entry SKIPs with a visible note (never a silent vacuous pass) and
a ``both`` entry grades its matcher part. The anti-circular guard
(:func:`~teatree.eval.corpus_grade.assert_independent_oracle`) is enforced on
every graded entry — a circular oracle is a FAIL row, so the grade exit code
catches it. :func:`grade_shipped_corpus` is the same deterministic body the
free ``corpus-grade`` lane in ``t3 eval all`` runs.

``show`` is privacy-safe by construction: it prints the label's own committed
fields plus DERIVED session counts — never a tool input, prompt text, or any
other raw session payload.
"""

import dataclasses
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from teatree.cli.eval.run_modes import make_grader
from teatree.cli.eval.verdict import LaneResult
from teatree.eval.corpus_grade import CircularOracleError, assert_independent_oracle, grade
from teatree.eval.corpus_loader import CORPUS_DIR, discover_corpus
from teatree.eval.corpus_models import CorpusLabel
from teatree.eval.report import JudgeGrader, ScenarioResult
from teatree.eval.session_transcript import SessionEvent, parse_session_jsonl

corpus_app = typer.Typer(help="Ground-truth corpus curation: list, inspect, and grade captured sessions.")

_DIR_HELP = "Corpus directory (default: the shipped corpus)."
_JUDGE_SKIP_NOTE = "skipped under --no-judge; re-run with --judge to grade"
_MATCHER_PART_NOTE = "matcher-part-only (--no-judge)"
_CIRCULAR_NOTE = "circular oracle — labelled_by is the rule author; relabel independently"


@dataclasses.dataclass(frozen=True)
class CorpusGradeRow:
    """One corpus entry's grading outcome — the table row and the ``corpus-grade`` lane substrate."""

    entry_id: str
    oracle: str
    verdict: str
    detail: str
    #: True when this entry carries a judge oracle that was requested but skipped
    #: (claude absent) — the judge graded nothing, so a green verdict is vacuous.
    judge_skipped: bool = False


def grade_corpus_rows(labels: list[CorpusLabel], *, directory: Path, judge: JudgeGrader | None) -> list[CorpusGradeRow]:
    """Grade each label's captured session into a :class:`CorpusGradeRow`.

    The anti-circular oracle guard runs first (a circular entry is a FAIL row);
    with no ``judge`` a judge-oracle entry SKIPs and a ``both`` entry grades its
    matcher part — free and deterministic.
    """
    return [_grade_row(label, directory=directory, judge=judge) for label in labels]


def grade_shipped_corpus() -> list[CorpusGradeRow]:
    """Grade the shipped corpus deterministically (no judge) — the free-lane body for ``t3 eval all``."""
    return grade_corpus_rows(discover_corpus(), directory=CORPUS_DIR, judge=None)


#: Surfaced when the corpus deterministically graded ZERO entries — the lane is
#: unrunnable for setup reasons (an all-judge / empty corpus under ``--no-judge``),
#: so it reads as needs-setup (``--strict`` fails on it), never a vacuous green.
CORPUS_NO_GRADED_HINT = (
    "no matcher-gradable corpus entries (all judge-oracle / empty) — add a matcher/both entry "
    "or run with --judge; nothing was deterministically validated"
)


def corpus_grade_lane(rows: list[CorpusGradeRow]) -> LaneResult:
    """Fold graded rows into the free ``corpus-grade`` lane for ``t3 eval all``.

    ``graded == 0`` (every entry judge-skipped, or an empty corpus) is NOT a
    green pass — there is nothing deterministically validated, so a green row
    would be vacuous. It surfaces as a setup-skip (``setup_hint`` set →
    ``needs_setup`` → ``--strict`` fails, the verdict flags it not-yet-validated),
    mirroring the AI lane's no-transcripts skip.
    """
    failed = sum(1 for row in rows if row.verdict == "fail")
    skipped = sum(1 for row in rows if row.verdict == "skip")
    graded = len(rows) - skipped
    detail = f"{graded} graded, {failed} failed, {skipped} judge-skipped"
    if graded == 0:
        return LaneResult(
            name="corpus-grade",
            cost="free",
            passed=True,
            skipped=True,
            detail=detail,
            setup_hint=CORPUS_NO_GRADED_HINT,
        )
    return LaneResult(
        name="corpus-grade",
        cost="free",
        passed=failed == 0,
        skipped=False,
        detail=detail,
    )


@corpus_app.command("list")
def list_entries(
    directory: Path | None = typer.Option(None, "--dir", help=_DIR_HELP),
) -> None:
    """List corpus entries: id, oracle, confidence, axis, expected outcome, labeller (sorted by id)."""
    labels = discover_corpus(directory)
    if not labels:
        typer.echo("(no corpus entries)")
        return
    Console().print(_build_corpus_table(labels))


@corpus_app.command("show")
def show(
    entry_id: str = typer.Argument(..., help="Corpus entry id to inspect."),
    directory: Path | None = typer.Option(None, "--dir", help=_DIR_HELP),
) -> None:
    """Show one label in full plus a privacy-safe session summary (counts only, never payloads)."""
    root = CORPUS_DIR if directory is None else directory
    label = _require_entry(discover_corpus(directory), entry_id)
    for line in _show_lines(label, _session_events(label, root)):
        typer.echo(line)


@corpus_app.command("grade")
def grade_entries(
    entry_id: str | None = typer.Argument(None, help="Corpus entry id to grade (omit to grade all)."),
    directory: Path | None = typer.Option(None, "--dir", help=_DIR_HELP),
    judge: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--judge/--no-judge",
        help=(
            "Grade judge-oracle entries with the LLM judge (metered). The --no-judge default is free "
            "and deterministic: judge entries SKIP with a note; `both` entries grade their matcher part."
        ),
    ),
    judge_budget: int = typer.Option(20, "--judge-budget", help="Max LLM-judge calls per run (cost cap)."),
) -> None:
    """Grade corpus entries against their ground-truth labels; any FAIL exits non-zero.

    Every entry passes :func:`~teatree.eval.corpus_grade.assert_independent_oracle`
    first — a matcher entry whose labeller is its rule author grades as a FAIL
    row rather than silently agreeing with itself.
    """
    root = CORPUS_DIR if directory is None else directory
    labels = discover_corpus(directory)
    if entry_id is not None:
        labels = [_require_entry(labels, entry_id)]
    if not labels:
        typer.echo("(no corpus entries)")
        return
    rows = grade_corpus_rows(labels, directory=root, judge=make_grader(enabled=judge, judge_budget=judge_budget))
    Console().print(_build_grade_table(rows))
    if any(row.verdict == "fail" for row in rows):
        sys.exit(1)
    if judge:
        # `--judge` asked for a metered judge run. If judge-oracle entries exist
        # but every one skipped (claude absent), the judge graded nothing — a
        # vacuous green. Fail loud rather than report it as passed (§4a).
        eligible = [row for row in rows if row.oracle in {"judge", "both"}]
        if eligible and all(row.judge_skipped for row in eligible):
            typer.echo(
                f"--judge requested and {len(eligible)} judge-oracle entr(y/ies) ran, but the judge graded "
                "0 of them — every judge call skipped (likely `claude` not on PATH). This fails loud rather "
                "than reporting a vacuous green; provision claude/CLAUDE_CODE_OAUTH_TOKEN or drop --judge.",
                err=True,
            )
            sys.exit(1)


def _grade_row(label: CorpusLabel, *, directory: Path, judge: JudgeGrader | None) -> CorpusGradeRow:
    try:
        assert_independent_oracle(label, judge_present=judge is not None)
    except CircularOracleError:
        return CorpusGradeRow(entry_id=label.entry_id, oracle=label.oracle, verdict="fail", detail=_CIRCULAR_NOTE)
    if label.oracle == "judge" and judge is None:
        return CorpusGradeRow(entry_id=label.entry_id, oracle=label.oracle, verdict="skip", detail=_JUDGE_SKIP_NOTE)
    result = grade(label, _session_events(label, directory), judge=judge)
    return CorpusGradeRow(
        entry_id=label.entry_id,
        oracle=label.oracle,
        verdict=result.verdict,
        detail=_detail(label, result, judge),
        judge_skipped=result.judge is not None and result.judge.skipped,
    )


def _detail(label: CorpusLabel, result: ScenarioResult, judge: JudgeGrader | None) -> str:
    parts: list[str] = []
    if result.matcher_results:
        failed = sum(1 for matcher in result.matcher_results if not matcher.passed)
        total = len(result.matcher_results)
        parts.append(f"{failed}/{total} matchers failed" if failed else f"{total} matcher(s) ok")
    if result.judge is not None:
        state = "skipped" if result.judge.skipped else ("pass" if result.judge.passed else "fail")
        parts.append(f"judge={state}")
    elif label.oracle == "both" and judge is None:
        parts.append(_MATCHER_PART_NOTE)
    return "; ".join(parts)


def _session_events(label: CorpusLabel, directory: Path) -> list[SessionEvent]:
    path = directory / f"{label.entry_id}.session.jsonl"
    return parse_session_jsonl(path.read_text(encoding="utf-8"))


def _require_entry(labels: list[CorpusLabel], entry_id: str) -> CorpusLabel:
    match = next((label for label in labels if label.entry_id == entry_id), None)
    if match is None:
        typer.echo(f"unknown corpus entry: {entry_id!r}", err=True)
        available = ", ".join(label.entry_id for label in labels) or "(none)"
        typer.echo(f"available entries: {available}", err=True)
        raise typer.Exit(code=2)
    return match


def _show_lines(label: CorpusLabel, events: list[SessionEvent]) -> list[str]:
    tool_calls = sum(1 for event in events if event.tool_name is not None)
    return [
        f"entry_id: {label.entry_id}",
        f"labelled_by: {label.labelled_by}",
        f"labelled_at: {label.labelled_at}",
        f"oracle: {label.oracle}",
        f"confidence: {label.confidence}",
        f"outcome_axis: {label.outcome_axis}",
        f"expected_outcome: {label.expected_outcome}",
        f"rule_author: {label.rule_author or '(unset)'}",
        f"source_session_id: {label.source_session_id or '(unset)'}",
        f"expected_behavior: {label.expected_behavior}",
        f"matchers: {len(label.matchers)}",
        f"judge_rubric: {'yes' if label.judge is not None else 'no'}",
        f"session_events: {len(events)}",
        f"session_tool_calls: {tool_calls}",
    ]


def _build_corpus_table(labels: list[CorpusLabel]) -> Table:
    table = Table(title="Ground-truth corpus", show_lines=False)
    table.add_column("Entry", style="bold")
    table.add_column("Oracle")
    table.add_column("Confidence")
    table.add_column("Axis")
    table.add_column("Expected")
    table.add_column("Labelled by")
    for label in labels:
        table.add_row(
            label.entry_id,
            label.oracle,
            label.confidence,
            label.outcome_axis,
            label.expected_outcome,
            label.labelled_by,
        )
    return table


def _build_grade_table(rows: list[CorpusGradeRow]) -> Table:
    table = Table(title="Corpus grade", show_lines=False)
    table.add_column("Entry", style="bold")
    table.add_column("Oracle")
    table.add_column("Verdict", justify="right")
    table.add_column("Detail")
    for row in rows:
        color = "yellow" if row.verdict == "skip" else ("green" if row.verdict == "pass" else "red")
        table.add_row(row.entry_id, row.oracle, f"[{color}]{row.verdict}[/{color}]", row.detail)
    return table

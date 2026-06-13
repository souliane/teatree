"""§17.6 enforcement-gate tool commands (#836).

Split out of ``cli/tools.py`` (which had reached the per-file
module-health public-function cap): the two deterministic gates in the
§17.6 enforcement-gate family — the PR-body / commit AI-signature scan
and the per-diff coverage + mutation/revert gate. Commands register onto
the shared ``tool_app`` so the user-facing CLI surface (``t3 tool
ai-sig-scan`` / ``t3 tool diff-coverage``) is unchanged; this mirrors
the ``triage_tools`` split.

Importing this module has the side effect of registering the commands;
``cli/__init__`` imports it after ``tool_app`` is constructed.
"""

import json
from pathlib import Path

import typer

from teatree.cli.tools import ToolRunner, tool_app


@tool_app.command("ai-sig-scan")
def ai_sig_scan(
    path: str = typer.Argument("-", help="File or '-' for stdin (PR body / commit message)"),
) -> None:
    """Refuse a PR body / commit message carrying an AI-signature trailer.

    Enforces the "No AI Signature on Posts Made on the User's Behalf" rule
    (BLUEPRINT §17.6 gate 15, #836) as deterministic code — previously prose
    only in /t3:rules and unenforced at the PR-body layer (PR #831 leak).
    """
    ToolRunner.run_script("ai_signature_scan", path)


def _source_is_newer(src: Path, cov_mtime: float) -> bool:
    """Whether *src* is strictly newer than the coverage file's mtime.

    A file removed mid-walk (``OSError`` on ``stat``) contributes nothing to
    the staleness decision rather than crashing the whole gate — the walk
    degrades gracefully without suppressing a genuine staleness signal from
    the files that are still present.
    """
    try:
        return src.stat().st_mtime > cov_mtime
    except OSError:
        return False


def _coverage_is_stale(coverage_file: Path, repo: Path) -> bool:
    try:
        cov_mtime = coverage_file.stat().st_mtime
        return any(_source_is_newer(src, cov_mtime) for src in repo.rglob("*.py") if src != coverage_file)
    except OSError:
        return False


@tool_app.command("diff-coverage")
def diff_coverage(
    *,
    repo: Path = typer.Option(Path.cwd, "--repo", help="Repo root (default: cwd)"),
    coverage_file: Path = typer.Option(Path(".coverage"), "--coverage-file", help="Path to .coverage data file"),
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Per-diff coverage + mutation/revert gate (BLUEPRINT §17.6 gate 12, #836).

    Measures coverage on the *diff's* added production lines (not the global
    ``fail_under``) and requires every new/changed production symbol to be
    imported by a changed test (the test-a-local-copy anti-vacuity check).
    Exits non-zero when a new line is uncovered or a symbol is unreferenced.
    """
    from teatree.utils.diff_coverage import measure_diff_coverage  # noqa: PLC0415
    from teatree.utils.git import full_worktree_diff  # noqa: PLC0415

    if not coverage_file.exists():
        typer.echo(
            f"WARNING: no coverage data at {coverage_file} — the per-diff "
            "line-coverage check measured nothing (only the symbol check ran). "
            "Run `uv run pytest` first for full enforcement.",
            err=True,
        )
    elif _coverage_is_stale(coverage_file, repo):
        typer.echo(
            f"WARNING: .coverage at {coverage_file} is stale (a source file is newer) — "
            "line-coverage results may be inaccurate. Run `uv run pytest` to refresh.",
            err=True,
        )

    diff = full_worktree_diff(str(repo))
    report = measure_diff_coverage(diff, coverage_data_file=coverage_file, repo_root=repo)
    if output_json:
        typer.echo(
            json.dumps(
                {
                    "passes": report.passes(),
                    "uncovered": [{"path": u.path, "lines": u.lines} for u in report.uncovered],
                    "unreferenced_symbols": report.unreferenced_symbols,
                }
            )
        )
    else:
        typer.echo(report.summary())
    if not report.passes():
        raise typer.Exit(code=1)

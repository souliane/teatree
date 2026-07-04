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
    base: str = typer.Option("origin/main", "--base", help="Ref to diff against (merge-base..HEAD)"),
    coverage_file: Path = typer.Option(Path(".coverage"), "--coverage-file", help="Path to .coverage data file"),
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Per-diff coverage + mutation/revert gate (BLUEPRINT §17.6 gate 12, #836).

    Measures coverage on the *branch's* added production lines — the committed
    diff against its merge-base with ``--base`` (default ``origin/main``), NOT the
    clone's working tree, so unrelated uncommitted edits never enter the gate.
    Requires every new/changed production symbol to be imported by a changed test
    (the test-a-local-copy anti-vacuity check). Exits non-zero when a new line is
    uncovered or a symbol is unreferenced.
    """
    from teatree.utils.diff_coverage import measure_diff_coverage  # noqa: PLC0415
    from teatree.utils.git import branch_diff  # noqa: PLC0415

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

    diff = branch_diff(str(repo), base)
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


@tool_app.command("gate-relaxation")
def gate_relaxation(
    *,
    repo: Path = typer.Option(Path.cwd, "--repo", help="Repo root (default: cwd)"),
    base: str = typer.Option("", "--base", help="Diff <merge-base>..HEAD against this ref instead of the staged diff."),
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Anti-relaxation + tach-soundness gate (BLUEPRINT §17.6.1/§17.6.2, #850).

    Refuses a diff that relaxes a lint/coverage constraint or a tach module
    boundary without a sanctioned relax marker: a new unjustified ``# noqa``, a
    new ``per-file-ignores`` / coverage ``omit`` entry, a lowered ``fail_under``,
    a committed ``--no-verify``, a new empty ``interfaces = []``, or a new
    ``ignore_type_checking_imports`` without a justifying comment. Only the
    diff's ADDED lines are inspected, so the pre-gate boilerplate baseline is
    exempt. Scans the STAGED diff by default; ``--base`` scans a branch range.
    Exits non-zero on any BLOCK finding; WARN findings (possible test vacuity)
    print advisory-only and never fail.
    """
    from teatree.quality.gate_relaxation import (  # noqa: PLC0415 — heavy import kept off the CLI cold path
        BLOCK,
        scan_relaxation,
    )
    from teatree.utils.git_commit import branch_diff  # noqa: PLC0415 — heavy import kept off the CLI cold path
    from teatree.utils.git_run import run as _git_run  # noqa: PLC0415 — heavy import kept off the CLI cold path

    if base:
        diff = branch_diff(str(repo), base)
    else:
        diff = _git_run(repo=str(repo), args=["diff", "--cached", "--src-prefix=a/", "--dst-prefix=b/"])
    findings = scan_relaxation(diff)
    blocking = [f for f in findings if f.severity == BLOCK]
    if output_json:
        typer.echo(
            json.dumps(
                {
                    "passes": not blocking,
                    "findings": [
                        {"kind": f.kind, "path": f.path, "severity": f.severity, "message": f.message, "line": f.line}
                        for f in findings
                    ],
                }
            )
        )
    else:
        for f in findings:
            typer.echo(f"{f.severity.upper()}: {f.path}: {f.message}", err=True)
        typer.echo("PASS" if not blocking else f"BLOCKED: {len(blocking)} relaxation finding(s)")
    if blocking:
        raise typer.Exit(code=1)

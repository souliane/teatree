"""``t3 tool comment-density`` — the standalone near-zero-comments diff gate.

Registers onto the shared ``tool_app`` (side-effect import from
``cli/__init__``, mirroring ``test_shape_tools`` / ``skill_ref_tools``). The
analysis lives in :mod:`teatree.hooks.privacy_diff_comment_density` (the same
content-blind density pass the pre-push privacy gate already uses); this module
is the reusable surface that the dedicated prek hook and the CI job both call,
so any overlay can adopt the check with one command.

The check is content-blind: it flags a file whose ADDED diff lines either
exceed a conservative comment:code ratio (with floors so a tiny diff or a
single explanatory comment never trips) OR carry a run of 3+ consecutive
comment-only lines. Tooling pragmas (``# type:``/``# noqa``/``# pragma`` /
``// eslint-disable`` / ``@ts-ignore`` …), docstrings, license/shebang headers,
``tests/`` and ``docs`` are exempt — the target is WHAT-narration comments
that merely restate the code.

Diff sources (first match wins): ``--diff <file>``, ``--staged``
(``git diff --cached``), ``--base-ref <ref>`` (the PR diff vs a base, used by
CI), else stdin. Exit ``0`` when clean, ``1`` when a file is flagged.
"""

import json
import sys
from pathlib import Path

import typer

from teatree.cli.tools import tool_app
from teatree.hooks.privacy_diff_comment_density import CommentDensityFinding, report_diff
from teatree.utils.run import run_allowed_to_fail


def _diff_from_staged() -> str:
    result = run_allowed_to_fail(
        ["git", "diff", "--cached", "--diff-filter=ACMR", "-U0"],
        expected_codes=None,
    )
    return result.stdout


def _diff_from_base_ref(base_ref: str) -> str:
    result = run_allowed_to_fail(
        ["git", "diff", "--diff-filter=ACMR", "-U0", f"{base_ref}...HEAD"],
        expected_codes=None,
    )
    return result.stdout


def _read_diff(diff_file: Path | None, base_ref: str | None, *, staged: bool) -> str:
    if diff_file is not None:
        return diff_file.read_text(encoding="utf-8")
    if staged:
        return _diff_from_staged()
    if base_ref is not None:
        return _diff_from_base_ref(base_ref)
    return sys.stdin.read()


def _render_findings(findings: list[CommentDensityFinding]) -> str:
    bullets = "\n".join(f"  - {f.render()}" for f in findings)
    return (
        "comment-density gate (near-zero-comments rule — names + types are the docs):\n\n"
        f"{bullets}\n\n"
        "These added comments restate what the code already says. Delete the\n"
        "WHAT-narration, or rename the symbols so the intent is self-evident.\n"
        "Genuine rationale belongs in the commit message; a deliberate\n"
        "threat-model note may use a `security:` comment prefix; tooling\n"
        "directives (# noqa / # type: / // eslint-disable …) are already exempt."
    )


@tool_app.command("comment-density")
def comment_density(
    *,
    diff_file: Path | None = typer.Option(
        None, "--diff", help="Read the unified diff from this file instead of stdin."
    ),
    staged: bool = typer.Option(False, "--staged", help="Scan `git diff --cached` (the pre-push / pre-commit diff)."),
    base_ref: str | None = typer.Option(
        None, "--base-ref", help="Scan the diff of HEAD vs this base ref (the PR diff; used by CI)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Flag added comments that merely restate the code (near-zero-comments rule).

    Content-blind density pass over a unified diff. Reusable by any overlay:
    the dedicated prek hook and the CI job both call this command. Exits ``1``
    when a file's added lines are comment-dense, ``0`` when clean. Never a
    PreToolUse gate, so it can never lock the agent's tools.
    """
    diff = _read_diff(diff_file, base_ref, staged=staged)
    findings = report_diff(diff)

    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "path": f.path,
                        "comment_lines": f.comment_lines,
                        "code_lines": f.code_lines,
                        "max_consecutive": f.max_consecutive,
                        "ratio": round(f.ratio, 3),
                        "reason": f.reason,
                    }
                    for f in findings
                ]
            )
        )
    elif findings:
        typer.echo(_render_findings(findings), err=True)
    else:
        typer.echo("comment-density: no findings (added comments stay near zero).")

    if findings:
        raise typer.Exit(code=1)

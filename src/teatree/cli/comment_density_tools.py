"""``t3 tool comment-density`` — the advisory near-zero-comments diff check.

Registers onto the shared ``tool_app`` (side-effect import from
``cli/__init__``, mirroring ``test_shape_tools`` / ``skill_ref_tools``). The
analysis lives in :mod:`teatree.hooks.privacy_diff_comment_density` (a
content-aware diff pass); this module is the reusable surface that the
dedicated prek hook and the CI job both call, so any overlay can adopt the
check with one command.

The check is content-aware: beyond a conservative comment:code ratio and a
consecutive comment-only run, it flags a comment whose words merely restate
the next code line and a docstring opening that merely echoes the signature
(a single such line is enough), with floors so a tiny diff or a lone
explanatory comment never trips. Tooling pragmas
(``# type:``/``# noqa``/``# pragma`` / ``// eslint-disable`` / ``@ts-ignore`` …),
docstrings carrying a genuine non-obvious why, license/shebang headers,
``tests/`` and ``docs`` are exempt — the target is WHAT-narration comments
that merely restate the code.

Diff sources (first match wins): ``--diff <file>``, ``--staged``
(``git diff --cached``), ``--base-ref <ref>`` (the PR diff vs a base, used by
CI), else stdin. The check is **advisory**: it prints the findings as a
warning but **always exits 0**, so it never blocks a commit, push, or
pipeline.
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
        "comment-density warning (near-zero-comments rule — names + types are the docs):\n\n"
        f"{bullets}\n\n"
        "These added comments may restate what the code already says. Consider\n"
        "deleting the WHAT-narration, or renaming the symbols so the intent is\n"
        "self-evident. Genuine rationale belongs in the commit message; a\n"
        "deliberate threat-model note may use a `security:` comment prefix;\n"
        "tooling directives (# noqa / # type: / // eslint-disable …) are already\n"
        "exempt. This is advisory only — nothing is blocked."
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
    """Warn on added comments that merely restate the code (comments-as-code rule).

    Content-aware diff pass over a unified diff. Reusable by any overlay:
    the dedicated prek hook and the CI job both call this command. The check
    is **advisory** — it prints the findings as a warning but **always exits
    0**, so it never blocks a commit, push, or pipeline, and it is never a
    PreToolUse gate.
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
                        "restatements": f.restatements,
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

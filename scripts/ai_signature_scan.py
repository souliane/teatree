"""Scan a PR body or commit message for AI-signature / banned trailers.

Enforces the "No AI Signature on Posts Made on the User's Behalf" rule
(BLUEPRINT §17.6 gate 15, #836) as deterministic code. The rule lived
only as prose in ``/t3:rules`` and was unenforced at the PR-body /
commit-message layer — PR #831 leaked the ``Generated with [Claude
Code]`` trailer, caught only by cold review.

Used by: the ``ai-sig-scan`` tool command and the pre-merge /
pr-create-time hook gate. Exit code 0 = clean, 1 = a banned trailer was
found.

Design — match trailer *position*, not a bare substring. A banned
pattern only trips when it appears as a *footer/trailer line*: the
match must start at the beginning of the (whitespace-stripped) line.
Prose that *describes* the banned trailer ("a ``Generated with [Claude
Code]`` footer") does not start the line with the pattern (it is
preceded by prose and/or wrapped in backticks), so the rule's own
documentation, /t3:rules, and BLUEPRINT do not self-block. A line whose
stripped form is fully inside an inline-code span is also exempt.
"""

import re
import sys
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)

# Each pattern is anchored at the START of the stripped line (trailer
# position). ``re.IGNORECASE`` so ``Co-authored-by`` / ``CO-AUTHORED-BY``
# variants are caught. The Co-Authored-By trailer is banned only when it
# names a model / Claude / Anthropic — a human co-author is legitimate.
_MODEL_AUTHOR = r"(?:claude|anthropic|gpt|opus|sonnet|haiku|copilot|\bai\b|noreply@anthropic)"

_TRAILER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "co-authored-by-model",
        re.compile(rf"^co-authored-by:\s*.*{_MODEL_AUTHOR}", re.IGNORECASE),
    ),
    (
        "generated-with",
        re.compile(r"^(?:\U0001f916\s*)?generated with\b", re.IGNORECASE),
    ),
    (
        "generated-with-claude-code",
        re.compile(r"^(?:\U0001f916\s*)?generated with \[claude code\]", re.IGNORECASE),
    ),
    (
        "via-claude",
        re.compile(r"^.{0,40}\b(?:via|using|with) claude\b\s*$", re.IGNORECASE),
    ),
    (
        "sent-using-claude",
        re.compile(r"^(?:sent|posted|created|written|drafted) (?:using|via|with|by) claude\b", re.IGNORECASE),
    ),
    (
        "emoji-bot-footer",
        re.compile(r"^\U0001f916\s*\S"),
    ),
]


def _strip_inline_code(line: str) -> str:
    """Blank out inline-code spans.

    A backticked *mention* of the trailer (documentation describing the
    banned pattern) must not trip the gate. The text between paired
    backticks is replaced with spaces of equal length so column offsets
    are preserved but the banned pattern inside a code span no longer
    matches as a line-leading trailer.
    """
    return re.sub(r"`[^`]*`", lambda m: " " * len(m.group()), line)


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """Return ``(lineno, category, matched_line)`` for every banned trailer.

    A line is only a finding when, after stripping leading whitespace and
    blanking inline-code spans, a banned pattern matches at the line start
    — i.e. the banned text is in trailer/footer position, not described in
    running prose.
    """
    findings: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        candidate = _strip_inline_code(raw).strip()
        if not candidate:
            continue
        for category, pattern in _TRAILER_PATTERNS:
            if pattern.match(candidate):
                findings.append((lineno, category, raw.strip()))
                break
    return findings


def _summary(findings: list[tuple[int, str, str]]) -> str:
    if not findings:
        return "AI-signature scan: clean (0 findings)"
    header = f"AI-signature scan: {len(findings)} banned trailer(s)"
    rows = [f"  line {ln}: {cat}: {text}" for ln, cat, text in findings]
    return "\n".join([header, *rows])


@app.command()
def main(
    input_file: str = typer.Argument("-", help="File to scan (- for stdin, or a file path)"),
    *,
    strict: bool = typer.Option(True, help="Exit 1 on any finding. --no-strict for warnings only."),
) -> None:
    """Scan a PR body / commit message for AI-signature / banned trailers."""
    text = sys.stdin.read() if input_file == "-" else Path(input_file).read_text(encoding="utf-8")
    findings = scan_text(text)
    print(_summary(findings))
    if findings and strict:
        raise SystemExit(1)


if __name__ == "__main__":
    app()

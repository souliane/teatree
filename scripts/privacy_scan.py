"""Scan text (typically a git diff) for privacy-sensitive patterns.

Used by: retro (§ Privacy Scan), contribute (§2 Pre-Flight).
Exit code 0 = clean, 1 = findings.
"""

import json
import re
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False)
console = Console(stderr=True)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", re.ASCII)
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/)[a-zA-Z0-9_.-]+")
_IP_RE = re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")
_API_KEY_RE = re.compile(r"\b(?:glpat-|sk-|ghp_|gho_|github_pat_|xoxb-|xoxp-)[a-zA-Z0-9_-]{10,}")
_HOSTNAME_RE = re.compile(r"\b[a-z0-9-]+\.internal\.[a-z]+\b|\b[a-z0-9-]+\.corp\.[a-z]+\b")
_FALSE_POSITIVE_RE = re.compile(r"example\.com|user@example|jane|bob|placeholder")


def _build_banned_re(banned_terms: str) -> re.Pattern[str] | None:
    terms = [t.strip() for t in banned_terms.split(",") if t.strip()]
    if not terms:
        return None
    escaped = [re.escape(t) for t in terms]
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


def _scan_line(line: str, banned_re: re.Pattern[str] | None) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    findings.extend(("email", m.group()) for m in _EMAIL_RE.finditer(line) if not _FALSE_POSITIVE_RE.search(m.group()))
    findings.extend(("home_path", m.group()) for m in _HOME_PATH_RE.finditer(line))
    findings.extend(("private_ip", m.group()) for m in _IP_RE.finditer(line))
    findings.extend(("api_key", m.group()[:20] + "...") for m in _API_KEY_RE.finditer(line))
    findings.extend(
        ("internal_hostname", m.group())
        for m in _HOSTNAME_RE.finditer(line)
        if not _FALSE_POSITIVE_RE.search(m.group())
    )
    if banned_re:
        findings.extend(("banned_term", m.group()) for m in banned_re.finditer(line))
    return findings


@app.command()
def main(
    input_file: str = typer.Argument("-", help="File to scan (- for stdin, or a file path)"),
    banned_terms: str = typer.Option("", envvar="T3_BANNED_TERMS", help="Comma-separated banned terms"),
    *,
    strict: bool = typer.Option(True, help="Strict mode (exit 1 on any finding). Use --no-strict for warnings only."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Scan text for privacy-sensitive patterns."""
    text = sys.stdin.read() if input_file == "-" else Path(input_file).read_text(encoding="utf-8")
    banned_re = _build_banned_re(banned_terms)
    all_findings: list[dict[str, str | int]] = []

    for lineno, line in enumerate(text.splitlines(), 1):
        all_findings.extend(
            {"line": lineno, "category": category, "match": match} for category, match in _scan_line(line, banned_re)
        )

    if json_output:
        print(json.dumps(all_findings, indent=2))
    elif all_findings:
        table = Table(title="Privacy Scan Findings")
        table.add_column("Line", style="dim", justify="right")
        table.add_column("Category", style="bold")
        table.add_column("Match")
        for f in all_findings:
            table.add_row(str(f["line"]), str(f["category"]), str(f["match"]))
        console.print(table)
    else:
        console.print("[green]Privacy scan: clean[/]")

    if all_findings and strict:
        raise SystemExit(1)

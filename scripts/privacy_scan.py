"""Scan text (typically a git diff) for privacy-sensitive patterns.

Used by: retro (§ Privacy Scan), contribute (§2 Pre-Flight), and the
public-repo pre-push leak gate (``scripts/hooks/refuse-public-push-with-leak.sh``).

Exit codes:

* ``0`` — clean (no findings), or ``--no-strict`` regardless of findings.
* :data:`PRIVACY_FINDINGS_EXIT_CODE` (``3``) — genuine findings present in
    strict mode. This is a DEDICATED code, distinct from the generic Python
    exception code (``1``) and the typer usage-error code (``2``), so the
    pre-push gate can block on *findings only* and fail OPEN on any other
    non-zero exit (a scanner crash, a missing script, an argparse error).
    Conflating "findings" with "crash" previously wedged every push closed
    whenever the scanner itself failed (#126 gap 3).
"""

import json
import re
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from teatree.hooks.privacy_diff_comments import scan_diff as _scan_diff_comments

_DIFF_DETECTORS = (_scan_diff_comments,)

# Dedicated "findings present" exit code. NOT 1 (generic exception) and NOT
# 2 (typer usage error) so the leak gate can distinguish a real finding from
# the scanner crashing. See module docstring.
PRIVACY_FINDINGS_EXIT_CODE = 3

app = typer.Typer(add_completion=False)
console = Console(stderr=True)

# Require a plausible local part: it may contain ``.+-`` internally but
# must *end* in an email-local char (alphanumeric/underscore) immediately
# before ``@``. This drops the decorator/attribute class of false
# positives — ``+@pytest.fixture`` (diff ``+`` as a fake local part),
# ``@app.route``, ``@module.attr`` — where the char before ``@`` is a diff
# marker, whitespace, or absent, while still matching genuine addresses
# whose local part ends in a real char (including a one-char local part).
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9_]([a-zA-Z0-9_.+-]*[a-zA-Z0-9_])?@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}",
    re.ASCII,
)
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/)[a-zA-Z0-9_.-]+")
_IP_RE = re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")
_API_KEY_RE = re.compile(r"\b(?:glpat-|sk-|ghp_|gho_|github_pat_|xoxb-|xoxp-)[a-zA-Z0-9_-]{10,}")
_HOSTNAME_RE = re.compile(r"\b[a-z0-9-]+\.internal\.[a-z]+\b|\b[a-z0-9-]+\.corp\.[a-z]+\b")
_FALSE_POSITIVE_RE = re.compile(r"example\.com|user@example|jane|bob|placeholder")

# An SSH git remote — ``git@<host>:<org>/<repo>(.git)`` — is transport
# syntax, never an email/PII. ``_EMAIL_RE`` matches the ``git@<host>``
# prefix, so any test or code carrying a normal SSH remote URL would
# otherwise trip the public-repo privacy gate (a recurring false
# positive). The SSH user is always literally ``git`` and the host is
# followed by ``:<path>`` — a real email never has a ``:path`` after the
# domain — so this is a tight, non-weakening exclusion.
_SSH_GIT_REMOTE_RE = re.compile(r"\bgit@[a-zA-Z0-9.-]+:[A-Za-z0-9._/~-]+")


def _is_ssh_git_remote(line: str, match: re.Match[str]) -> bool:
    """True when an email match is actually the ``git@host`` of an SSH remote."""
    return any(rm.start() <= match.start() and match.end() <= rm.end() for rm in _SSH_GIT_REMOTE_RE.finditer(line))


# Inline allow-annotation, mirroring gitleaks' ``gitleaks:allow`` idiom.
# A line carrying this literal marker is exempt from all findings — used
# so a repo's own privacy-scanner test fixtures and the gate's own
# documentation examples do not self-block the public-repo privacy gate.
# It exempts only the line it appears on; a real leak on any other line
# is still reported.
_ALLOW_MARKER = "privacy-scan:allow"


def _build_banned_re(banned_terms: str) -> re.Pattern[str] | None:
    terms = [t.strip() for t in banned_terms.split(",") if t.strip()]
    if not terms:
        return None
    escaped = [re.escape(t) for t in terms]
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


def _scan_line(line: str, banned_re: re.Pattern[str] | None) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    if _ALLOW_MARKER in line:
        return findings
    findings.extend(
        ("email", m.group())
        for m in _EMAIL_RE.finditer(line)
        if not _FALSE_POSITIVE_RE.search(m.group()) and not _is_ssh_git_remote(line, m)
    )
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


def _run_diff_detectors(text: str) -> list[dict[str, str | int]]:
    """Run each whole-text diff detector fail-open.

    The diff detectors need the unified-diff structure (file headers + ``+``
    markers), so they run over the whole text rather than per line. A
    detector that raises is cannot-evaluate — it is skipped (a warning is
    emitted), NEVER a deny that wedges the push closed. This mirrors the
    gate-overdeny rule the per-line scan and the pre-push gate already
    follow: only genuine findings block, a crash never does.
    """
    findings: list[dict[str, str | int]] = []
    for detector in _DIFF_DETECTORS:
        try:
            hits = detector(text)
        except Exception as exc:  # noqa: BLE001 — fail-open: a crashing detector is cannot-evaluate, never a deny.
            name = getattr(detector, "__name__", repr(detector))
            console.print(f"[yellow]privacy scan: detector {name} failed ({exc}) — skipped[/]")
            continue
        findings.extend({"line": lineno, "category": category, "match": match} for lineno, category, match in hits)
    return findings


def _plain_summary(findings: list[dict[str, str | int]]) -> str:
    """Deterministic plain-text findings summary for non-TTY callers.

    Stable, greppable, line-oriented (one finding per line: line number,
    category, redacted match). Consumed verbatim by the pre-push gate's
    refusal message and by any scripted caller of ``t3 tool
    privacy-scan``. The redaction of the match itself is already applied
    upstream in ``_scan_line`` (api keys are truncated to a 20-char
    prefix); other categories carry the raw match by design so the user
    can locate the offending text.
    """
    if not findings:
        return "Privacy scan: clean (0 findings)"
    header = f"Privacy scan: {len(findings)} finding(s)"
    rows = [f"  line {f['line']}: {f['category']}: {f['match']}" for f in findings]
    return "\n".join([header, *rows])


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

    all_findings.extend(_run_diff_detectors(text))
    all_findings.sort(key=lambda f: int(f["line"]))

    if json_output:
        print(json.dumps(all_findings, indent=2))
    else:
        # Always emit a deterministic, plain-text summary on **stdout**.
        # This is the stream a piped/non-TTY caller reliably sees: the
        # pre-push gate captures it with ``> report 2>&1`` and
        # ``ToolRunner.run_script`` re-emits it. The rich table is a
        # TTY-only nicety on stderr; it must never be the *only* output,
        # or scripted callers get "exit 1, no diagnostics" (#696).
        print(_plain_summary(all_findings))
        if all_findings:
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
        raise SystemExit(PRIVACY_FINDINGS_EXIT_CODE)


if __name__ == "__main__":
    app()

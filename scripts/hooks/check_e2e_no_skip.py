"""Pre-push / CI gate: refuse a staged E2E spec carrying a skip/quarantine marker.

Background: an E2E spec that ships with ``test.skip(`` / ``test.only(`` /
``test.fixme(`` (or the mocha-style ``it.skip(`` / ``xit(`` /
``describe.skip(``) silently removes browser-level coverage — the suite reads
green while a scenario is disabled. A ``// TODO`` / ``// FIXME`` left inside a
spec body is the same class: a parked acceptance scenario that never runs.

This gate is deterministic and greppable — no judgment, no false positives on
non-spec files. It scans only staged spec files under an ``e2e/`` directory
(``e2e/**/*.spec.ts`` plus any overlay ``.../e2e/**/*.spec.ts``) and fails
closed with a ``file:line`` for every marker, so the agent removes/replaces the
skip before the push lands.

On a no-spec / clean-spec diff the hook is silent. Only a marker-bearing spec
prints findings and exits non-zero.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: In scope: ``.spec.ts`` under any ``e2e/`` dir (top-level or overlay-nested).
_SPEC_PATH_RE = re.compile(r"(?:^|/)e2e/.*\.spec\.ts$")

#: Skip/quarantine markers; ``\b`` + trailing ``(`` avoid ``skipLink`` substring hits.
_MARKER_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("test.skip(", re.compile(r"\btest\s*\.\s*skip\s*\(")),
    ("test.only(", re.compile(r"\btest\s*\.\s*only\s*\(")),
    ("test.fixme(", re.compile(r"\btest\s*\.\s*fixme\s*\(")),
    ("it.skip(", re.compile(r"\bit\s*\.\s*skip\s*\(")),
    ("xit(", re.compile(r"\bxit\s*\(")),
    ("describe.skip(", re.compile(r"\bdescribe\s*\.\s*skip\s*\(")),
)

#: ``// TODO`` / ``// FIXME`` line comment; the ``//`` excludes a "TODO list" string literal.
_BODY_COMMENT_RE = re.compile(r"//\s*(?:TODO|FIXME)\b")


@dataclass(frozen=True)
class Finding:
    """A single skip/quarantine marker located at ``path:line``."""

    path: str
    line: int
    marker: str

    @property
    def message(self) -> str:
        return (
            f"{self.path}:{self.line} — `{self.marker}` disables E2E coverage. "
            f"Remove or replace it; a skipped/quarantined spec ships a green suite "
            f"that guards nothing."
        )


def is_spec_path(path: str) -> bool:
    """True when ``path`` is an E2E spec the gate scans."""
    return bool(_SPEC_PATH_RE.search(path))


def scan_spec_lines(path: str, lines: list[str]) -> list[Finding]:
    """Return one :class:`Finding` per marker/body-comment in ``lines``."""
    findings: list[Finding] = []
    for index, raw in enumerate(lines, start=1):
        for marker, pattern in _MARKER_RES:
            if pattern.search(raw):
                findings.append(Finding(path=path, line=index, marker=marker))
        if _BODY_COMMENT_RE.search(raw):
            findings.append(Finding(path=path, line=index, marker="// TODO|FIXME"))
    return findings


def scan_specs(paths: list[str], *, root: Path) -> list[Finding]:
    """Scan each in-scope spec path under ``root``; missing files are skipped."""
    findings: list[Finding] = []
    for path in paths:
        if not is_spec_path(path):
            continue
        file_path = root / path
        try:
            text = file_path.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError):
            continue
        findings.extend(scan_spec_lines(path, text.splitlines()))
    return findings


def _run_git(cmd: list[str]) -> str:
    """Run a git query, FAIL-LOUD on a non-zero exit.

    ``check=False`` would let a git failure return an empty string, which
    ``main()`` reads as "no staged specs" and silently exits 0 — the gate
    fake-green. A ``CalledProcessError`` instead crashes with a diagnostic.
    """
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return result.stdout


def _staged_spec_paths() -> list[str]:
    out = _run_git(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    return [line for line in out.splitlines() if line and is_spec_path(line)]


def _format_failure(findings: list[Finding]) -> str:
    bullets = "\n".join(f"  - {f.message}" for f in findings)
    return (
        "E2E no-skip gate:\n\n"
        f"{bullets}\n\n"
        "A skipped/quarantined E2E scenario (or a parked // TODO/// FIXME in a\n"
        "spec body) removes browser-level coverage while the suite reads green.\n"
        "Remove the marker, finish the scenario, or move it to a separate PR."
    )


def main() -> int:
    paths = _staged_spec_paths()
    if not paths:
        return 0
    findings = scan_specs(paths, root=Path.cwd())
    if not findings:
        return 0
    print(_format_failure(findings))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

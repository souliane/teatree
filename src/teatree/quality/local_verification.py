"""Every per-phase local-verification mandate must name the diff-scoped lane (#3994).

A ticket reached a merged PR in ~3.5h against a 30m target because the whole suite ran
LOCALLY twice — once inside the coding phase, once before the push — before CI ran it a
third time on the PR. Only the third run gates the merge; the first two were advisory,
serial, and on the critical path, on a box where two concurrent agents already peaked at
14.85 GB.

The selector that scopes a run to the diff already exists
(:mod:`teatree.quality.affected_tests`, ``dev/test-affected.sh``) and already drives
``dev/ci-parity-fast.sh``. What drifted is the AGENT-FACING prose and dispatch brief that
tell a builder what to run — a skill that still mandates ``uv run pytest`` is what an agent
actually executes. This is the deterministic backstop for that prose.

Each registered surface must satisfy BOTH halves, because the negative half alone is
absence-satisfied — deleting the block would "pass" it:

- negative: it prescribes no UNSCOPED whole-``testpaths`` pytest invocation;
- positive: it NAMES a scoped lane, so the mandate still exists.

Scoping is not a weakened gate. ``classify_selection`` is fail-safe TO FULL — a migration,
a conftest / ``factories.py`` / test-settings edit, any unclassifiable executable path, a
missing merge-base, or a change to the selection machinery itself all escalate to the whole
suite — and the push gate plus CI's sharded whole-tree lane are untouched.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.full_suite_invocation import declared_testpaths, runs_full_suite

#: Tokens that name a diff-scoped verification lane. Any ONE satisfies the positive half.
SCOPED_LANE_TOKENS: tuple[str, ...] = (
    "dev/test-affected.sh",
    "dev/ci-parity-fast.sh",
    "t3 tool affected-tests",
)

#: Line-scoped escape for a command a surface CITES rather than prescribes. "never a bare
#: ``pytest``" and "a raw ``uv run pytest`` reports different counts" are textually identical
#: to a prescription, so no matcher can separate them — without this the guard would force
#: correct prose to be mangled. Line-scoped on purpose: it silences one line, never the file,
#: and the positive half (must NAME a scoped lane) is outside its reach entirely.
CITED_NOT_PRESCRIBED = "local-verification: cited-not-prescribed"


@dataclass(frozen=True)
class PhaseMandate:
    """A surface that tells an agent how to verify locally before pushing."""

    surface: str
    phase: str


#: DATA, not logic — adding a surface is a one-row edit. Every entry is a place a
#: dispatched agent reads to decide what to run in a phase.
PHASE_MANDATES: tuple[PhaseMandate, ...] = (
    PhaseMandate(surface="CLAUDE.md", phase="every phase (repo-root agent instructions)"),
    PhaseMandate(surface="AGENTS.md", phase="every phase (repo-root agent instructions)"),
    PhaseMandate(surface="README.md", phase="onboarding (quick-start command index)"),
    PhaseMandate(surface="docs/contributing.md", phase="onboarding (contributor quick-start)"),
    PhaseMandate(surface="tests/README.md", phase="testing (test-strategy command index)"),
    PhaseMandate(surface="skills/code/SKILL.md", phase="coding"),
    PhaseMandate(surface="skills/test/SKILL.md", phase="testing"),
    PhaseMandate(surface="skills/ship/SKILL.md", phase="shipping"),
    PhaseMandate(surface="skills/contribute/SKILL.md", phase="contributing"),
    PhaseMandate(surface="skills/retro/references/commit-to-fork.md", phase="contributing (fork pre-flight)"),
    PhaseMandate(surface="src/teatree/agents/coding_prompt.py", phase="coding (headless dispatch brief)"),
    PhaseMandate(surface="tests/CLAUDE.md", phase="coding (tests-tree conventions)"),
)


@dataclass(frozen=True)
class Finding:
    surface: str
    phase: str
    detail: str

    def __str__(self) -> str:
        return f"{self.surface} [{self.phase}]: {self.detail}"


#: A markdown inline-code span. Linear (no backtracking) — this runs over whole skill files.
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_FENCE = "```"


def command_candidates(text: str) -> list[str]:
    """The command texts a reader could copy: fenced-block bodies and inline-code spans.

    Prose is excluded on purpose. The matcher treats every trailing token as a positional,
    so a sentence continuing past an inline command — "…with its tests in the same commit" —
    reads as ``pytest tests`` and false-flags a correctly-scoped line. A prescription an
    agent copies is always written as code, so code spans are the whole surface. Extracting
    inline spans separately also strips the delimiters: shlex keeps a closing backtick
    attached, so ``uv run pytest`` written inline tokenises as ``pytest` `` and hides.

    A line carrying :data:`CITED_NOT_PRESCRIBED` is dropped whole. Each line is tokenised
    independently downstream, so dropping one never lets the next line's args be absorbed.
    """
    fenced: list[str] = []
    inline: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(_FENCE):
            in_fence = not in_fence
        elif CITED_NOT_PRESCRIBED in line:
            continue
        elif in_fence:
            fenced.append(line)
        else:
            inline.extend(_INLINE_CODE.findall(line))
    return ["\n".join(fenced), *inline]


def scan_text(text: str, mandate: PhaseMandate, roots: tuple[str, ...]) -> list[Finding]:
    """Both halves of the contract for one surface's *text*."""
    findings: list[Finding] = []
    if any(runs_full_suite(candidate, roots) for candidate in command_candidates(text)):
        findings.append(
            Finding(
                surface=mandate.surface,
                phase=mandate.phase,
                detail=(
                    "prescribes an UNSCOPED whole-suite pytest run — the local run must be "
                    f"diff-scoped (one of {', '.join(SCOPED_LANE_TOKENS)}); CI's sharded lane is the authority"
                ),
            )
        )
    if not any(token in text for token in SCOPED_LANE_TOKENS):
        findings.append(
            Finding(
                surface=mandate.surface,
                phase=mandate.phase,
                detail=f"names no diff-scoped lane — expected one of {', '.join(SCOPED_LANE_TOKENS)}",
            )
        )
    return findings


def scan_mandates(root: Path, mandates: tuple[PhaseMandate, ...] = PHASE_MANDATES) -> list[Finding]:
    """Scan every registered mandate under *root*.

    An unreadable or absent surface is itself a finding: a rename that silently emptied the
    registry would otherwise read as a clean scan.
    """
    roots = declared_testpaths(root / "pyproject.toml")
    findings: list[Finding] = []
    for mandate in mandates:
        path = root / mandate.surface
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            findings.append(Finding(surface=mandate.surface, phase=mandate.phase, detail=f"unreadable ({exc})"))
            continue
        findings.extend(scan_text(text, mandate, roots))
    return findings

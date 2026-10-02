r"""Built-in terminology gate for teatree-internal vocabulary.

Unlike the brand/tenant ``banned_terms`` lists (operator-supplied via
the teatree bootstrap TOML config / ``$TEATREE_BANNED_BRANDS``), these phrases are
teatree's own internal vocabulary rules — they ship with the public repo
and apply to the repo itself, so they live in code with a fixed
correction message per phrase rather than in operator config.

The headline rule distinguishes two stores that are routinely conflated.
A *teatree task* is a row in the DB-backed ``Task`` model — a claimable
lifecycle work unit with a phase, lease, and ticket. A *harness TODO* is
the agent harness's own working list (the ``TaskCreate`` / ``TaskUpdate``
items, formerly ``TodoWrite``).

``teatree todo`` is never correct (there is no such store), and
``Claude TODO`` is harness-specific where teatree stays harness-agnostic —
both steer to the right term for the context.

The gate runs inside the full-tree backstop scan
(``teatree.core.banned_terms_tree.scan_committed_tree``), so it is
exercised by the same ``t3 banned-terms scan-tree`` CI job as the brand
backstop. It carries its own carve-out: a line that is itself stating the
rule (it mentions the correct term alongside the banned phrase) is allowed
so this module, its tests, and the skill prose that documents the rule do
not self-trip.
"""

import re
from dataclasses import dataclass

# Each rule: a compiled case-insensitive pattern and the steering correction.
# ``\bClaude\s+TODO`` also matches ``Claude Code TODO`` via the optional
# ``Code`` token. Plural ``s?`` on both stores.


@dataclass(frozen=True)
class TerminologyRule:
    pattern: re.Pattern[str]
    correction: str


_CORRECTION = "use 'teatree task' (DB Task model) or 'harness TODO' (harness list)"

_RULES: tuple[TerminologyRule, ...] = (
    TerminologyRule(re.compile(r"\bteatree[ _-]todos?\b", re.IGNORECASE), _CORRECTION),
    TerminologyRule(re.compile(r"\bClaude(?:[ _-]Code)?[ _-]todos?\b", re.IGNORECASE), _CORRECTION),
)

# A line that pairs the banned phrase WITH its corrected term is the rule
# itself being documented (this module, its tests, the skill prose), not a
# conflation — allow it so the gate does not flag its own definition.
_CARVE_OUT = re.compile(r"\bteatree task\b|\bharness TODO\b", re.IGNORECASE)


@dataclass(frozen=True)
class TerminologyFinding:
    """A single conflated-terminology hit on one line."""

    phrase: str
    correction: str


def scan_line(line: str) -> list[TerminologyFinding]:
    """Return the conflated-terminology hits on *line*, honoring the carve-out."""
    if _CARVE_OUT.search(line):
        return []
    findings: list[TerminologyFinding] = []
    for rule in _RULES:
        match = rule.pattern.search(line)
        if match is not None:
            findings.append(TerminologyFinding(match.group(0), rule.correction))
    return findings


def scan_text(text: str) -> list[tuple[int, TerminologyFinding]]:
    """Scan *text* line by line; return ``(lineno, finding)`` hits."""
    hits: list[tuple[int, TerminologyFinding]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        hits.extend((lineno, finding) for finding in scan_line(line))
    return hits


# The gate's own definition and its test necessarily quote the banned
# phrases verbatim; exempting them by path lets the rule document itself
# without self-tripping. The per-line carve-out covers the documentation
# elsewhere (skill prose, this module's docstring lines that pair the
# phrase with its correction).
_EXEMPT_SUFFIXES: tuple[str, ...] = (
    "src/teatree/hooks/terminology_gate.py",
    "tests/teatree_hooks/test_terminology_gate.py",
)


def path_is_exempt(rel_path: str) -> bool:
    """Whether *rel_path* (POSIX, repo-relative) is the gate's own source/test."""
    return any(rel_path.endswith(suffix) for suffix in _EXEMPT_SUFFIXES)

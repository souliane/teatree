"""Anti-relaxation + tach-soundness gate engine (BLUEPRINT §17.6.1/§17.6.2, #850).

Scans a unified diff for the two failure modes §17.6 protects against:
incremental relaxation of lint/coverage constraints, and tach configurations
that pass their own check while enforcing no real module boundary. The engine
inspects only the diff's ADDED lines, so the "boilerplate baseline" (entries
present before the gate was deployed) is exempt for free — a pre-existing
suppression is never in the added set, only a NEW one is a finding.

Two severities feed the §17.6.5 WARN-not-hardfail doctrine: a :data:`BLOCK`
finding comes from a clean-separating deterministic matcher (a new unjustified
``# noqa``, a new ``omit`` entry, a lowered ``fail_under``, a committed
``--no-verify``, a new empty ``interfaces = []``) and refuses the commit unless
the sanctioned relax marker is present; a :data:`WARN` finding comes from a
fuzzy heuristic (possible test vacuity) and is advisory-only. The consumer
(``t3 tool gate-relaxation`` / the ``gate-relaxation`` prek hook) decides
deny-vs-warn from the severity; this module only classifies.
"""

import re
from dataclasses import dataclass
from typing import Final

BLOCK: Final = "block"
WARN: Final = "warn"

# A diff ``+++ b/<path>`` header. The leading ``b/`` prefix git emits is
# stripped so ``path`` is the repo-relative file path.
_DIFF_NEW_FILE_RE: Final[re.Pattern[str]] = re.compile(r"^\+\+\+ b/(.+)$")
_DIFF_OLD_FILE_RE: Final[re.Pattern[str]] = re.compile(r"^--- a/(.+)$")

# A real ``noqa`` trailing suppression: ``noqa`` or ``noqa: <codes>`` with an
# optional trailing justification. Group ``codes`` is the comma/space code list
# (empty for a bare marker); group ``rest`` is the free text after the codes
# (the justification, when non-empty).
_NOQA_RE: Final[re.Pattern[str]] = re.compile(r"#\s*noqa(?::\s*(?P<codes>[A-Z0-9, ]*?))?(?:\s+(?P<rest>\S.*))?$")

# A complexity-suppression code: McCabe C901 or the too-many-* Pylint refactor
# family PLR09xx (PLR0911 return-count, PLR0912 branch-count, PLR0913 arg-count,
# PLR0915 statement-count, PLR0916 boolean-expr-count, PLR0917 positional-count).
_COMPLEXITY_CODE_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:C901|PLR09\d\d)\b")

# Single-line string-literal spans (optional r/b/f/u prefix). Blanked before the
# suppression scan so the gate never matches a ``noqa`` marker that lives INSIDE
# a string literal — e.g. this module's own regex-source matcher definitions.
_STRING_SPAN_RE: Final[re.Pattern[str]] = re.compile(r"""[rRbBfFuU]{0,2}('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")

# Config files whose ADDED lines carry lint/coverage relaxation. noqa lives in
# ``.py`` sources; per-file-ignores / omit / fail_under live in these.
_LINT_COV_CONFIG_FILES: Final[frozenset[str]] = frozenset(
    {"pyproject.toml", "ruff.toml", ".ruff.toml", "setup.cfg", ".coveragerc", "tox.ini"}
)


@dataclass(frozen=True)
class RelaxationFinding:
    """One detected relaxation. ``severity`` is :data:`BLOCK` or :data:`WARN`."""

    kind: str
    path: str
    severity: str
    message: str
    line: str = ""


@dataclass(frozen=True)
class _FileDiff:
    """Added/removed line text for one file in a unified diff."""

    path: str
    added: list[str]
    removed: list[str]


def _blank_string_literals(line: str) -> str:
    """Return ``line`` with single-line string-literal contents blanked to spaces.

    So a ``# noqa`` inside a string literal (this module's own matcher source,
    a test fixture) is not mistaken for a real trailing suppression comment.
    """
    return _STRING_SPAN_RE.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], line)


def parse_diff(diff: str) -> list[_FileDiff]:
    """Group a unified diff into per-file added / removed line text.

    Diff and index/hunk metadata lines (``+++``/``---``/``@@``) are excluded;
    only genuine ``+``/``-`` body lines are collected, with the leading marker
    stripped. A file with neither added nor removed lines is omitted.
    """
    by_path: dict[str, _FileDiff] = {}
    current: _FileDiff | None = None
    for raw in diff.splitlines():
        new_match = _DIFF_NEW_FILE_RE.match(raw)
        if new_match:
            path = new_match.group(1)
            current = by_path.setdefault(path, _FileDiff(path=path, added=[], removed=[]))
            continue
        if _DIFF_OLD_FILE_RE.match(raw) or raw.startswith(("diff ", "index ", "@@")):
            continue
        if current is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            current.added.append(raw[1:])
        elif raw.startswith("-") and not raw.startswith("---"):
            current.removed.append(raw[1:])
    return [fd for fd in by_path.values() if fd.added or fd.removed]


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _noqa_findings(fd: _FileDiff) -> list[RelaxationFinding]:
    """Flag new ``# noqa`` suppressions in an added ``.py`` line.

    A ``# noqa`` is a finding when it is a REAL trailing comment (code precedes
    the ``#`` on the line, after blanking string literals) AND either it carries
    no justification text (:data:`BLOCK`) or it suppresses a complexity code
    C901 / PLR09xx (:data:`BLOCK` regardless of justification — the fix is to
    split the function, not to silence the check).
    """
    if not fd.path.endswith(".py"):
        return []
    findings: list[RelaxationFinding] = []
    for line in fd.added:
        blanked = _blank_string_literals(line)
        hash_idx = blanked.find("#")
        if hash_idx < 0 or not blanked[:hash_idx].strip():
            continue  # no code before the comment (or no comment) — not a real suppression
        match = _NOQA_RE.search(blanked[hash_idx:])
        if not match:
            continue
        codes = (match.group("codes") or "").strip()
        justification = (match.group("rest") or "").strip()
        if _COMPLEXITY_CODE_RE.search(codes):
            findings.append(
                RelaxationFinding(
                    kind="complexity_suppression",
                    path=fd.path,
                    severity=BLOCK,
                    message=f"new complexity suppression `noqa: {codes}` — refactor instead of silencing",
                    line=line.strip(),
                )
            )
        elif not justification:
            findings.append(
                RelaxationFinding(
                    kind="noqa_without_justification",
                    path=fd.path,
                    severity=BLOCK,
                    message="new `noqa` with no inline justification — say why suppression is correct here",
                    line=line.strip(),
                )
            )
    return findings


def _lint_cov_findings(fd: _FileDiff) -> list[RelaxationFinding]:
    """Flag new per-file-ignores / coverage-omit entries and a lowered fail_under."""
    if _basename(fd.path) not in _LINT_COV_CONFIG_FILES:
        return []
    findings: list[RelaxationFinding] = []
    for line in fd.added:
        stripped = line.strip()
        if "per-file-ignores" in stripped:
            findings.append(
                RelaxationFinding(
                    kind="per_file_ignore_added",
                    path=fd.path,
                    severity=BLOCK,
                    message="new `per-file-ignores` entry relaxes lint enforcement for a whole file glob",
                    line=stripped,
                )
            )
        if _line_adds_coverage_omit(stripped):
            findings.append(
                RelaxationFinding(
                    kind="coverage_omit_added",
                    path=fd.path,
                    severity=BLOCK,
                    message="new coverage `omit` entry removes a file from coverage measurement",
                    line=stripped,
                )
            )
    findings.extend(_fail_under_findings(fd))
    return findings


def _line_adds_coverage_omit(stripped: str) -> bool:
    """Whether an added config line introduces or extends a coverage ``omit`` list.

    Matches both the inline-table form (``omit = [ "…" ]``) and a bare glob
    entry added to a multi-line ``omit`` array (a quoted path ending in ``,``
    or ``]`` — a source-glob element, not arbitrary quoted config).
    """
    if re.match(r"omit\s*=", stripped):
        return True
    return bool(re.match(r'["\'][^"\']+["\']\s*,?\s*\]?$', stripped) and "*" in stripped)


def _fail_under_findings(fd: _FileDiff) -> list[RelaxationFinding]:
    """Flag a lowered coverage ``fail_under`` floor (removed value > added value)."""
    added = _fail_under_value(fd.added)
    removed = _fail_under_value(fd.removed)
    if added is not None and removed is not None and added < removed:
        return [
            RelaxationFinding(
                kind="coverage_floor_lowered",
                path=fd.path,
                severity=BLOCK,
                message=f"coverage `fail_under` lowered from {removed} to {added}",
                line=f"fail_under = {added}",
            )
        ]
    return []


def _fail_under_value(lines: list[str]) -> float | None:
    for line in lines:
        match = re.match(r"\s*fail_under\s*=\s*([0-9]+(?:\.[0-9]+)?)", line)
        if match:
            return float(match.group(1))
    return None


def _no_verify_findings(fd: _FileDiff) -> list[RelaxationFinding]:
    """Flag a committed ``--no-verify`` in a shell / Makefile / CI file.

    Scoped to executable-config surfaces (``.sh``/``.bash``, a ``Makefile``,
    a ``.github/`` or CI ``.yml``/``.yaml``) so a Python string mentioning the
    flag — this gate's own source, a test fixture, a docstring — never trips it.
    """
    base = _basename(fd.path)
    is_shell = fd.path.endswith((".sh", ".bash")) or base in {"Makefile", "makefile", "GNUmakefile"}
    is_ci = (fd.path.endswith((".yml", ".yaml")) and (".github/" in fd.path or "ci" in fd.path.lower())) or (
        base == ".pre-commit-config.yaml"
    )
    if not (is_shell or is_ci):
        return []
    return [
        RelaxationFinding(
            kind="no_verify_added",
            path=fd.path,
            severity=BLOCK,
            message="committed `--no-verify` bypasses the git hook chain",
            line=line.strip(),
        )
        for line in fd.added
        if "--no-verify" in line
    ]


def _tach_findings(fd: _FileDiff) -> list[RelaxationFinding]:
    """Flag unsound tach edits in the diff.

    A new empty ``interfaces = []`` on a touched module (tach then enforces no
    encapsulation), or a new ``ignore_type_checking_imports = true`` with no
    justifying comment. Per the §17.6.2 phasing, only DIFF-ADDED lines are
    inspected: a pre-existing empty interface (the root default) is untouched;
    only a newly-declared one on a module added/modified in this diff is a
    finding.
    """
    if _basename(fd.path) != "tach.toml":
        return []
    findings: list[RelaxationFinding] = []
    has_added_comment = any(line.lstrip().startswith("#") for line in fd.added)
    for line in fd.added:
        stripped = line.strip()
        if re.match(r"interfaces\s*=\s*\[\s*\]", stripped):
            findings.append(
                RelaxationFinding(
                    kind="empty_interfaces_added",
                    path=fd.path,
                    severity=BLOCK,
                    message="new `interfaces = []` declares an empty public API — tach then enforces no encapsulation",
                    line=stripped,
                )
            )
        if re.match(r"ignore_type_checking_imports\s*=\s*true", stripped) and not has_added_comment:
            findings.append(
                RelaxationFinding(
                    kind="type_check_ignore_without_comment",
                    path=fd.path,
                    severity=BLOCK,
                    message="new `ignore_type_checking_imports = true` with no comment justifying it",
                    line=stripped,
                )
            )
    return findings


# An added test function whose added body carries an assertion token is not
# vacuous. Assertions include bare ``assert``, unittest ``self.assert*`` /
# ``self.fail``, ``pytest.raises`` / ``pytest.warns``, and ``raise`` (a
# must-raise contract test).
_ASSERTION_RE: Final[re.Pattern[str]] = re.compile(
    r"\bassert\b|self\.assert|self\.fail|pytest\.(?:raises|warns)|\braise\b"
)
_TEST_DEF_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:async\s+)?def\s+test_\w*\s*\(")


def _test_vacuity_findings(fd: _FileDiff) -> list[RelaxationFinding]:
    """WARN when an added ``def test_…`` has no assertion token in its added block.

    A fuzzy heuristic (an assertion via a called helper is invisible here), so it
    is WARN-only per §17.6.5 — advisory, never a hard deny. It fires only when a
    test function is ADDED in this diff and the added lines up to the next
    top-level ``def`` carry no assertion token.
    """
    if not (fd.path.endswith(".py") and ("test" in _basename(fd.path) or "/tests/" in fd.path)):
        return []
    if not any(_TEST_DEF_RE.match(line) for line in fd.added):
        return []
    if _ASSERTION_RE.search("\n".join(fd.added)):
        return []
    return [
        RelaxationFinding(
            kind="possible_test_vacuity",
            path=fd.path,
            severity=WARN,
            message="an added test function has no visible assertion in the diff — confirm it is not vacuous",
        )
    ]


def scan_relaxation(diff: str) -> list[RelaxationFinding]:
    """Return every relaxation finding in ``diff`` (BLOCK and WARN), or ``[]``.

    Vacuous-on-empty: an empty diff yields no findings. Each per-file check is
    scoped by file kind so a matcher never fires on an unrelated surface (a
    ``--no-verify`` string in a ``.py`` file, a ``# noqa`` inside a string
    literal). The consumer refuses the commit on any BLOCK finding absent the
    sanctioned relax marker and prints WARN findings advisory-only.
    """
    findings: list[RelaxationFinding] = []
    for fd in parse_diff(diff):
        findings.extend(_noqa_findings(fd))
        findings.extend(_lint_cov_findings(fd))
        findings.extend(_no_verify_findings(fd))
        findings.extend(_tach_findings(fd))
        findings.extend(_test_vacuity_findings(fd))
    return findings

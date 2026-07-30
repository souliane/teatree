"""Anti-relaxation + tach-soundness gate engine (BLUEPRINT §17.6.1/§17.6.2, #850).

Scans a unified diff for the two failure modes §17.6 protects against:
incremental relaxation of lint/coverage constraints, and tach configurations
that pass their own check while enforcing no real module boundary. Findings key
off the diff's ADDED lines, so the "boilerplate baseline" (entries present
before the gate was deployed) is exempt for free. For ``# noqa`` suppressions
the scan is diff-aware (§17.6.2): an added suppression is paired with its
base-ref version by code stem, so a code already suppressed on that line is not
a NEW relaxation when it reappears because sibling codes were stripped (ruff
RUF100) — only a genuinely-newly-introduced suppression code is a finding.

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

# The start of a real ``noqa`` trailing suppression marker. The code list and
# any justification after it are parsed token-by-token in
# :func:`_parse_noqa_line`, so a multi-code marker (``noqa: F401, E501``) is not
# mis-split into one code plus a spurious "justification".
_NOQA_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"#\s*noqa\b")

# One ruff/flake8 suppression code token: uppercase-letter prefix + digits
# (E501, F401, PLR0913, C901, ARG002, PLR6301). The code list after ``noqa:`` is
# these tokens comma-separated; the first non-code text is the justification.
_NOQA_CODE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z]+[0-9]+")

# A justification is meaningful only when it says WHY — a single trailing token
# is not a reason, so ANY trailing text no longer passes. It must reference an
# issue id (``#3313``) OR carry enough word-characters to be a real explanation.
_MIN_JUSTIFICATION_WORD_CHARS: Final[int] = 8
_ISSUE_REF_RE: Final[re.Pattern[str]] = re.compile(r"#\d+")


def _is_meaningful_justification(text: str) -> bool:
    stripped = text.strip()
    if _ISSUE_REF_RE.search(stripped):
        return True
    return sum(char.isalnum() for char in stripped) >= _MIN_JUSTIFICATION_WORD_CHARS


# A complexity-suppression code: McCabe C901 or the too-many-* Pylint refactor
# family PLR09xx (PLR0911 return-count, PLR0912 branch-count, PLR0913 arg-count,
# PLR0915 statement-count, PLR0916 boolean-expr-count, PLR0917 positional-count).
_COMPLEXITY_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^(?:C901|PLR09\d\d)$")

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
    """Added/removed line text for one file in a unified diff.

    ``added_in_omit[i]`` mirrors ``added[i]``: True when that added line sits
    inside a coverage ``omit`` array/list, tracked from the diff's context so an
    element of an unrelated ``exclude``/``include`` array is never mistaken for a
    coverage omit.
    """

    path: str
    added: list[str]
    removed: list[str]
    added_in_omit: list[bool]


def blank_string_literals(line: str) -> str:
    """Return ``line`` with single-line string-literal contents blanked to spaces.

    So a ``# noqa`` inside a string literal (this module's own matcher source,
    a test fixture) is not mistaken for a real trailing suppression comment.
    Public so the sibling ship-chain ``debt_delta`` scanner reuses the same
    real-trailing-comment primitive rather than duplicating the regex.
    """
    return _STRING_SPAN_RE.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], line)


def parse_diff(diff: str) -> list[_FileDiff]:
    """Group a unified diff into per-file added / removed line text.

    Diff and index/hunk metadata lines (``+++``/``---``/``@@``) are excluded;
    only genuine ``+``/``-`` body lines are collected, with the leading marker
    stripped. A ``+++ /dev/null`` header (a deleted file) resets the accumulator so
    the deleted file's ``-`` lines never bleed into the previous file's removed
    bucket. Each added line records whether it sits inside a coverage ``omit``
    array (``added_in_omit``), tracked from the new-file line sequence (context +
    added lines) so the coverage-omit matcher is array-context aware. A file with
    neither added nor removed lines is omitted.
    """
    by_path: dict[str, _FileDiff] = {}
    current: _FileDiff | None = None
    in_omit = False
    for raw in diff.splitlines():
        new_match = _DIFF_NEW_FILE_RE.match(raw)
        if new_match:
            path = new_match.group(1)
            current = by_path.setdefault(path, _FileDiff(path=path, added=[], removed=[], added_in_omit=[]))
            in_omit = False
            continue
        if raw.startswith("+++"):  # a `+++` header that is not `+++ b/<path>` (i.e. `/dev/null`, a deleted file)
            current = None
            in_omit = False
            continue
        if _DIFF_OLD_FILE_RE.match(raw) or raw.startswith(("diff ", "index ")):
            continue
        if raw.startswith("@@"):
            in_omit = False  # a hunk gap — the enclosing array is unknown again
            continue
        if current is None:
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            current.removed.append(raw[1:])  # removed lines are not in the new file; they never move omit state
            continue
        if raw.startswith("+"):  # `+++` headers were already handled above
            body = raw[1:]
            current.added.append(body)
            current.added_in_omit.append(in_omit)
            in_omit = _advance_omit_list(body, in_omit=in_omit)
            continue
        if raw.startswith(" "):  # a context line — present in the new file, tracks omit state
            in_omit = _advance_omit_list(raw[1:], in_omit=in_omit)
    return [fd for fd in by_path.values() if fd.added or fd.removed]


# A config assignment (INI ``key =`` / TOML ``key = [``) and a section/table
# header. Used to track whether the parser cursor sits inside a coverage ``omit``
# list across a diff hunk's context + added lines, so a quoted glob is judged by
# its ENCLOSING array rather than in isolation.
_ASSIGN_RE: Final[re.Pattern[str]] = re.compile(r"""^(?P<key>[A-Za-z0-9_.\-"']+?)\s*=\s*(?P<val>.*)$""")
_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^\[.*\]$")


def _advance_omit_list(line: str, *, in_omit: bool) -> bool:
    """Return whether the cursor is inside a coverage ``omit`` list AFTER *line*.

    Handles the TOML array form (``omit = [ … ]``, closed by ``]``) and the INI
    multi-line form (``omit =`` then indented values, ended by a blank line, a new
    ``[section]``, or a new ``key =`` assignment). A non-``omit`` assignment (a ruff
    ``exclude``/``extend-exclude``, a coverage ``source``) ends the omit list, so
    its elements are never counted as omit entries.
    """
    stripped = line.strip()
    if not stripped:
        return False  # a blank line ends an INI multi-line list
    if _SECTION_RE.match(stripped):
        return False  # a new `[section]`/table ends any list
    assign = _ASSIGN_RE.match(stripped)
    if assign:
        key = assign.group("key").strip("\"'").rsplit(".", 1)[-1]
        if key == "omit":
            val = assign.group("val")
            return "[" not in val or "]" not in val  # an inline `omit = [ … ]` closes; INI/open-TOML stays inside
        return False  # a different assignment ends any omit list
    if in_omit:
        return "]" not in stripped  # a TOML array element; a `]` closes the array
    return in_omit


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class _NoqaOnLine:
    """One parsed ``# noqa`` suppression on a source line.

    ``stem`` is the code text preceding the ``#`` (stripped) — the line's symbol
    identity, used to pair an added suppression with its base-ref version across
    a diff. ``codes`` is the parsed suppression-code set; ``justified`` is whether
    a MEANINGFUL justification follows the codes (:func:`_is_meaningful_justification`
    — an issue ref or enough word-characters, not a single trailing token).
    """

    stem: str
    codes: frozenset[str]
    justified: bool


def _parse_noqa_line(line: str) -> _NoqaOnLine | None:
    """Parse a REAL trailing ``# noqa`` suppression out of one source line.

    Returns ``None`` when the line has no real trailing suppression — no code
    before the ``#`` (a pure comment line), or no ``noqa`` marker. String
    literals are blanked first so a ``noqa`` inside a string is never read as a
    suppression. The comma-separated code list is parsed token-by-token, so a
    multi-code marker (``# noqa: F401, E501``) yields the full code set and an
    empty justification — not one code plus a spurious "justification".
    """
    blanked = blank_string_literals(line)
    hash_idx = blanked.find("#")
    if hash_idx < 0:
        return None
    stem = blanked[:hash_idx].strip()
    if not stem:
        return None  # pure comment line — no code before the marker
    marker = _NOQA_MARKER_RE.match(blanked[hash_idx:])
    if not marker:
        return None
    rest = blanked[hash_idx + marker.end() :]
    codes: set[str] = set()
    if rest.startswith(":"):
        rest = rest[1:]
        while (token := _NOQA_CODE_TOKEN_RE.match(rest.lstrip())) is not None:
            codes.add(token.group(0))
            rest = rest.lstrip()[token.end() :]
            if rest.lstrip().startswith(","):
                rest = rest.lstrip()[1:]
            else:
                break
    return _NoqaOnLine(stem=stem, codes=frozenset(codes), justified=_is_meaningful_justification(rest))


def _base_noqa_by_stem(removed: list[str]) -> dict[str, _NoqaOnLine]:
    """Map each removed line's code-stem to its parsed base-ref ``# noqa``.

    The base-ref suppression for a symbol: a code already present here is not a
    NEW relaxation when it reappears on the added version of the same line — the
    noqa merely changed because sibling codes were stripped (ruff RUF100).
    """
    parsed = (_parse_noqa_line(line) for line in removed)
    return {p.stem: p for p in parsed if p is not None}


def _noqa_findings(fd: _FileDiff) -> list[RelaxationFinding]:
    """Flag NEW ``# noqa`` suppressions in an added ``.py`` line, diff-aware.

    A ``# noqa`` on an added line is a finding only when it introduces a NEW
    relaxation relative to the base-ref version of the same line (paired by code
    stem across the diff, §17.6.2): a complexity code C901 / PLR09xx not already
    suppressed on that line (:data:`BLOCK` regardless of justification — the fix
    is to split the function), or a newly-suppressed or newly-unjustified code
    with no justification (:data:`BLOCK`). A pre-existing code that reappears
    because sibling codes were stripped is not a new relaxation.
    """
    if not fd.path.endswith(".py"):
        return []
    base_by_stem = _base_noqa_by_stem(fd.removed)
    findings: list[RelaxationFinding] = []
    for line in fd.added:
        added = _parse_noqa_line(line)
        if added is None:
            continue
        base = base_by_stem.get(added.stem)
        base_codes = base.codes if base is not None else frozenset()
        new_codes = added.codes - base_codes
        if any(_COMPLEXITY_CODE_RE.match(code) for code in new_codes):
            findings.append(
                RelaxationFinding(
                    kind="complexity_suppression",
                    path=fd.path,
                    severity=BLOCK,
                    message="new complexity suppression `noqa` — refactor instead of silencing",
                    line=line.strip(),
                )
            )
        elif not added.justified and (base is None or new_codes or base.justified):
            findings.append(
                RelaxationFinding(
                    kind="noqa_without_justification",
                    path=fd.path,
                    severity=BLOCK,
                    message=(
                        "new `noqa` with no adequate justification — a bare token is not a reason; "
                        "explain WHY (reference an issue id, e.g. #NNNN, or a real explanation)"
                    ),
                    line=line.strip(),
                )
            )
    return findings


def _lint_cov_findings(fd: _FileDiff) -> list[RelaxationFinding]:
    """Flag new per-file-ignores / coverage-omit entries and a lowered fail_under."""
    if _basename(fd.path) not in _LINT_COV_CONFIG_FILES:
        return []
    findings: list[RelaxationFinding] = []
    for line, inside_omit in zip(fd.added, fd.added_in_omit, strict=True):
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
        if _line_adds_coverage_omit(stripped, inside_omit=inside_omit):
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


def _line_adds_coverage_omit(stripped: str, *, inside_omit: bool) -> bool:
    """Whether an added config line introduces or extends a coverage ``omit`` list.

    Flags the ``omit`` assignment itself (INI ``omit =`` or TOML ``omit = [ … ]``)
    and, when the line sits INSIDE an ``omit`` array/list (``inside_omit``, tracked
    from the diff's context), a bare glob/path element. The enclosing-array check
    stops a quoted glob inside an unrelated ``exclude``/``include`` array (a ruff
    config in the same ``pyproject.toml``) from being counted as a coverage omit.
    """
    if re.match(r"omit\s*=", stripped):
        return True
    return inside_omit and _is_list_entry(stripped)


def _is_list_entry(stripped: str) -> bool:
    """Whether *stripped* is a non-empty list element (not a bare bracket or blank)."""
    return bool(stripped.rstrip("],").strip().strip("\"'"))


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
    justifying comment. Per §17.6.2, only DIFF-ADDED lines are inspected: a
    pre-existing empty interface (the root default) is untouched; only a
    newly-declared one on a module added/modified in this diff is a finding.
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

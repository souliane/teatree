"""Pure net-new-tech-debt scanner + plan-manifest waiver logic (north-star PR-3).

The ship-chain sibling of :mod:`teatree.quality.gate_relaxation`. Where that
engine is a COMMIT-time / prek gate keyed on the §17.6 doctrine and an inline
``relax:`` marker, this scanner backs the MERGE-time ``debt_delta_gate`` in the
``_run_ship_gates`` chain and its escape is an AUDITED plan-manifest waiver
(``approved_debt``). Both share one diff parser: this module reuses
``gate_relaxation.parse_diff`` and ``blank_string_literals`` rather than
duplicating them.

Delta, not absolute: only diff-ADDED lines are scanned, so pre-existing (legacy)
debt on unchanged context is never flagged and removing a suppression is always
allowed — a shrink-only ratchet, the same property the deferred-import-peg and
module-health ledgers rely on. The signals mechanize CLAUDE.md's "no tech debt
without explicit approval":

- a new ``# noqa`` trailing suppression;
- a new ``# type: ignore`` trailing suppression;
- a new ``# pragma: no cover`` coverage exclusion;
- a ``pytest.mark.skip`` / ``skipif`` / ``xfail`` with no tracking-ticket reference;
- a new ``per-file-ignores`` entry (header or glob->codes row) in a ruff config;
- a lowered coverage ``fail_under`` floor in ``pyproject.toml`` or a ``dev/*.sh`` script.

A ``DebtWaiver`` from the ratified plan manifest (pattern + non-empty reason)
lets a genuinely-justified introduction through — the approval recorded as a
durable artifact, never a silent bypass.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from teatree.quality.gate_relaxation import blank_string_literals, parse_diff

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class DebtIntroduction:
    """One net-new tech-debt suppression a diff introduces.

    ``kind`` is the signal class (``noqa`` / ``type_ignore`` / ``pragma_no_cover``
    / ``test_skip`` / ``per_file_ignore`` / ``coverage_floor_drop``); ``line`` is
    the offending added line (or a synthesized description for a floor drop);
    ``detail`` carries the before/after floor for a ``coverage_floor_drop``.
    """

    kind: str
    path: str
    line: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DebtWaiver:
    """A plan-manifest ``approved_debt`` entry — the audited debt-gate escape.

    ``pattern`` is matched (case-insensitive substring) against an introduction's
    line, its ``kind``, or its ``path``; ``reason`` is the recorded justification.
    A blank ``reason`` covers nothing — an audited escape must say why.
    """

    pattern: str
    reason: str


# A real trailing/standalone suppression comment: the marker directly follows a
# ``#`` (so a prose comment that merely mentions the term never matches), and the
# scan runs on a string-literal-blanked line (so a marker inside a string is inert).
_NOQA_RE: Final[re.Pattern[str]] = re.compile(r"#\s*noqa\b", re.IGNORECASE)
_TYPE_IGNORE_RE: Final[re.Pattern[str]] = re.compile(r"#\s*type:\s*ignore\b")
_PRAGMA_NO_COVER_RE: Final[re.Pattern[str]] = re.compile(r"#\s*pragma:\s*no\s+cover\b")
_MARKER_KINDS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (_NOQA_RE, "noqa"),
    (_TYPE_IGNORE_RE, "type_ignore"),
    (_PRAGMA_NO_COVER_RE, "pragma_no_cover"),
)

_TEST_SKIP_RE: Final[re.Pattern[str]] = re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)\b")
# A tracking reference on the skip line (``#1234`` or an issue URL) makes the
# skip tracked debt, not silent debt — so it is exempt.
_TICKET_REF_RE: Final[re.Pattern[str]] = re.compile(r"#\d+|/issues/\d+")

_RUFF_CONFIG_FILES: Final[frozenset[str]] = frozenset({"pyproject.toml", "ruff.toml", ".ruff.toml"})
_PER_FILE_IGNORES_RE: Final[re.Pattern[str]] = re.compile(r"per-file-ignores")
# A per-file-ignore row added under an existing table (no header in the diff):
# a quoted glob-ish key (a ``*``/``/``/``.py`` marks it a path glob, not a plain
# key) assigned to a list of codes.
_PFI_ENTRY_RE: Final[re.Pattern[str]] = re.compile(r'^\s*"[^"]*(?:\*|\.py|/)[^"]*"\s*=\s*\[')

# The coverage floor value in either the TOML ``fail_under = N`` form or the shell
# ``--cov-fail-under=N`` / ``--fail-under N`` form.
_FLOOR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:fail_under\s*=|--cov-fail-under[= ]|--fail-under[= ])\s*(\d+(?:\.\d+)?)"
)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _suppression_findings(path: str, added: list[str]) -> list[DebtIntroduction]:
    """New ``# noqa`` / ``# type: ignore`` / ``# pragma: no cover`` on added ``.py`` lines."""
    if not path.endswith(".py"):
        return []
    findings: list[DebtIntroduction] = []
    for line in added:
        blanked = blank_string_literals(line)
        findings.extend(
            DebtIntroduction(kind=kind, path=path, line=line.strip())
            for regex, kind in _MARKER_KINDS
            if regex.search(blanked)
        )
    return findings


def _test_skip_findings(path: str, added: list[str]) -> list[DebtIntroduction]:
    """A ``pytest.mark.skip``/``skipif``/``xfail`` with no tracking reference on an added line."""
    if not path.endswith(".py"):
        return []
    return [
        DebtIntroduction(kind="test_skip", path=path, line=line.strip())
        for line in added
        if _TEST_SKIP_RE.search(blank_string_literals(line)) and not _TICKET_REF_RE.search(line)
    ]


def _per_file_ignore_findings(path: str, added: list[str]) -> list[DebtIntroduction]:
    """A new ``per-file-ignores`` header or glob->codes entry in a ruff config file."""
    if _basename(path) not in _RUFF_CONFIG_FILES:
        return []
    return [
        DebtIntroduction(kind="per_file_ignore", path=path, line=line.strip())
        for line in added
        if _PER_FILE_IGNORES_RE.search(line) or _PFI_ENTRY_RE.match(line)
    ]


def _floor_value(lines: list[str]) -> float | None:
    for line in lines:
        match = _FLOOR_RE.search(line)
        if match:
            return float(match.group(1))
    return None


def _coverage_floor_findings(path: str, added: list[str], removed: list[str]) -> list[DebtIntroduction]:
    """A lowered coverage floor (removed value > added value) in pyproject / a ``.sh`` script."""
    if not (_basename(path) == "pyproject.toml" or path.endswith(".sh")):
        return []
    new_floor = _floor_value(added)
    old_floor = _floor_value(removed)
    if new_floor is None or old_floor is None or new_floor >= old_floor:
        return []
    span = f"{old_floor:g} -> {new_floor:g}"
    return [DebtIntroduction(kind="coverage_floor_drop", path=path, line=f"coverage floor {span}", detail=span)]


def scan_debt_delta(diff_text: str) -> list[DebtIntroduction]:
    """Every net-new tech-debt suppression in ``diff_text`` (added lines only), or ``[]``.

    Vacuous on a clean or empty diff. Each per-file check is file-kind scoped, so
    a matcher never fires on an unrelated surface (a ``per-file-ignores`` string
    in a ``.py`` file, a floor pattern outside a config/script).
    """
    introductions: list[DebtIntroduction] = []
    for fd in parse_diff(diff_text):
        introductions.extend(_suppression_findings(fd.path, fd.added))
        introductions.extend(_test_skip_findings(fd.path, fd.added))
        introductions.extend(_per_file_ignore_findings(fd.path, fd.added))
        introductions.extend(_coverage_floor_findings(fd.path, fd.added, fd.removed))
    return introductions


def load_debt_waivers(adequacy: object) -> tuple[DebtWaiver, ...]:
    """Read the ``approved_debt`` waivers off a plan-adequacy manifest.

    Each entry needs a non-blank ``pattern`` AND ``reason`` — a reasonless waiver
    is dropped here so it can never silently cover an introduction. Empty on a
    missing or malformed manifest.
    """
    if not isinstance(adequacy, dict):
        return ()
    raw = cast("Mapping[str, object]", adequacy).get("approved_debt")
    if not isinstance(raw, list):
        return ()
    waivers: list[DebtWaiver] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        fields = cast("Mapping[str, object]", entry)
        pattern = str(fields.get("pattern", "")).strip()
        reason = str(fields.get("reason", "")).strip()
        if pattern and reason:
            waivers.append(DebtWaiver(pattern=pattern, reason=reason))
    return tuple(waivers)


def waiver_covers(waiver: DebtWaiver, introduction: DebtIntroduction) -> bool:
    """Whether *waiver* (a reasoned pattern) covers *introduction*.

    A blank reason or blank pattern covers nothing. Otherwise the pattern is a
    case-insensitive substring of the introduction's line, equals its kind, or is
    a substring of its path.
    """
    if not waiver.reason.strip():
        return False
    needle = waiver.pattern.strip().casefold()
    if not needle:
        return False
    return (
        needle in introduction.line.casefold()
        or needle == introduction.kind.casefold()
        or needle in introduction.path.casefold()
    )


def unwaived_debt(
    introductions: list[DebtIntroduction],
    waivers: tuple[DebtWaiver, ...],
) -> list[DebtIntroduction]:
    """The introductions no waiver covers — the net-new debt the gate refuses."""
    return [intro for intro in introductions if not any(waiver_covers(w, intro) for w in waivers)]

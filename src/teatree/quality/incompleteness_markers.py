"""Scan the tree for prose in which code declares its own incompleteness.

The failure class: a change ships, CI is green, and the shipped module's own
docstring says the feature is not finished. Nothing looked for that sentence, so
the phase was declared complete and the gap survived review.

The source of truth for the phrases is ``incompleteness_markers.yaml`` next to
this module: families of SHAPES rather than literal strings, feeding both the
shrink-only ratchet in ``tests/quality/test_incompleteness_marker_ratchet.py``
and its closed-issue sub-gate. Keeping them in YAML is what lets this module
scan itself -- the ratchet reads comments and docstrings of ``.py`` files, so a
pattern table written inline here would make the detector its own largest
offender.

The registry serves two gates at two strengths. Over the code roots it is the
shrink-only ratchet above: every family, prose and marker alike, counted against
a per-file peg. Over the verification trees (``VERIFICATION_ROOTS``) it is an
outright ban with no ledger -- ``DeferredMarkerBan`` -- narrowed to the families the
registry marks ``form: marker`` and to markers that OPEN a comment. A test
carrying a deferred marker is a test that is not finished, so there is no count
to bank; and a marker opening a comment is the one form unambiguous enough to
red a build on, since a guard's own tests must be free to name the strings the
guard detects.

What counts as prose -- comments and docstrings, never a string literal -- is
``teatree.quality.prose``'s question; this module asks only which of those lines
declare the code unfinished.
"""

import dataclasses
import enum
import re
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

import yaml

from teatree.quality.prose import COMMENT_SUFFIXES, ProseLine, file_comments, file_prose

#: Directories whose ``.py`` files are scanned: everything that ships as code.
CODE_ROOTS: tuple[str, ...] = ("src/teatree", "hooks", "scripts")

#: Markdown scanned alongside the code. The architecture spec is the one doc
#: surface where a promise is a commitment the tree is read against; `docs/`
#: at large is release notes, how-to guides, and machine-generated reference,
#: where the same phrase carries no claim about shipped behaviour.
DOC_ROOTS: tuple[str, ...] = ("docs/blueprint",)
DOC_FILES: tuple[str, ...] = ("BLUEPRINT.md",)

#: One-shot dated design documents, out of scope by owner decision: they are
#: cleanup candidates under CLAUDE.md's "no historical narration" rule, and
#: their volume would swing the pegged count for reasons unrelated to code.
#: `test_scope.py` pins that each stays unscanned, so widening `DOC_ROOTS`
#: cannot silently pull one in.
ONE_SHOT_DESIGN_DOCS: tuple[str, ...] = ("docs/evals/sota-eval-runner.md",)

#: How many characters from a deferral phrase an issue reference still reads as
#: that phrase's tracking pointer. Beyond this the number is a citation of the
#: change that introduced the code, which says nothing about what is owed.
#: Sized to span one markdown issue link, whose URL separates the visible `#N`
#: from the surrounding words.
ISSUE_REF_PROXIMITY = 80

#: Trees holding verification artefacts rather than shipped behaviour. A marker
#: here records work a test did not do, and the marker is the only thing that
#: records it, so the marker forms are banned outright rather than pegged.
VERIFICATION_ROOTS: tuple[str, ...] = ("tests", "evals", "e2e")

#: What a comment's own syntax puts before its first word: the sigil, a markdown
#: bullet or heading or quote marker, and the leading dash a marker convention
#: also allows. Stripping it is what lets the ban ask whether the marker OPENS
#: the comment rather than appearing somewhere inside a sentence.
_COMMENT_OPENER_RE = re.compile(r"^[\s#>*+\-]*")

_ISSUE_REF_RE = re.compile(r"#(\d{1,6})\b")


class MarkerForm(enum.StrEnum):
    """Whether a family matches an author's note or the shape of a sentence."""

    MARKER = "marker"
    PROSE = "prose"


class MarkerRegistryError(ValueError):
    """The YAML registry is malformed."""

    def __init__(self, entry_id: str | None, problem: str) -> None:
        where = f"entry {entry_id!r}" if entry_id else "registry"
        super().__init__(f"{where}: {problem}")


@dataclasses.dataclass(frozen=True)
class MarkerPattern:
    id: str
    name: str
    concern: str
    remedy: str
    regex: re.Pattern[str]
    triggers: tuple[str, ...]
    form: MarkerForm = MarkerForm.PROSE


@dataclasses.dataclass(frozen=True)
class Marker:
    path: str
    lineno: int
    pattern_id: str
    remedy: str
    phrase: str
    text: str
    issue_refs: tuple[int, ...]

    def describe(self) -> str:
        return (
            f"  - {self.path}:{self.lineno} [{self.pattern_id}] {self.phrase!r} in: {self.text}\n    -> {self.remedy}"
        )


def registry_path() -> Path:
    return Path(__file__).parent / "incompleteness_markers.yaml"


def _require_str(entry: dict[str, Any], key: str, entry_id: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MarkerRegistryError(entry_id, f"{key!r} must be a non-empty string")
    return value


def load_marker_patterns(path: Path | None = None) -> tuple[MarkerPattern, ...]:
    raw = yaml.safe_load((path or registry_path()).read_text(encoding="utf-8"))
    entries = raw.get("markers") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise MarkerRegistryError(None, "must carry a non-empty 'markers' list")
    patterns: list[MarkerPattern] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise MarkerRegistryError(None, "each marker entry must be a mapping")
        entry_id = _require_str(entry, "id", "<unknown>")
        if entry_id in seen:
            raise MarkerRegistryError(entry_id, "duplicate marker id")
        seen.add(entry_id)
        triggers = entry.get("triggers")
        if not isinstance(triggers, list) or not all(isinstance(word, str) and word for word in triggers):
            raise MarkerRegistryError(entry_id, "'triggers' must be a non-empty list of strings")
        try:
            form = MarkerForm(entry.get("form", MarkerForm.PROSE))
        except ValueError as unknown:
            raise MarkerRegistryError(
                entry_id, f"'form' must be one of {[member.value for member in MarkerForm]}"
            ) from unknown
        patterns.append(
            MarkerPattern(
                id=entry_id,
                name=_require_str(entry, "name", entry_id),
                concern=_require_str(entry, "concern", entry_id),
                remedy=_require_str(entry, "remedy", entry_id),
                # Case-insensitive by default: this tree emphasises with capitals,
                # and the docstring that motivated the gate shouted both of its
                # admissions that way.
                regex=re.compile(
                    _require_str(entry, "pattern", entry_id),
                    flags=0 if entry.get("case_sensitive") else re.IGNORECASE,
                ),
                triggers=tuple(word.lower() for word in triggers),
                form=form,
            )
        )
    return tuple(patterns)


def issue_refs_near(text: str, span: tuple[int, int]) -> tuple[int, ...]:
    """Issue numbers in *text* close enough to *span* to read as its tracking pointer."""
    start, end = span
    refs = {
        int(match.group(1))
        for match in _ISSUE_REF_RE.finditer(text)
        if match.start() < end + ISSUE_REF_PROXIMITY and match.end() > start - ISSUE_REF_PROXIMITY
    }
    return tuple(sorted(refs))


def scanned_files(repo_root: Path) -> list[Path]:
    excluded = {repo_root / rel for rel in ONE_SHOT_DESIGN_DOCS}
    found: list[Path] = []
    for rel, suffix in [(root, "*.py") for root in CODE_ROOTS] + [(root, "*.md") for root in DOC_ROOTS]:
        root_path = repo_root / rel
        if root_path.is_dir():
            found.extend(sorted(root_path.rglob(suffix)))
    found.extend(repo_root / rel for rel in DOC_FILES)
    return [path for path in found if path.is_file() and path not in excluded]


def _windows(lines: Sequence[ProseLine]) -> Iterator[tuple[ProseLine, str]]:
    """Each prose line joined with its successor, so a wrapped phrase stays whole.

    Docstrings and comment blocks wrap mid-sentence, and the docstring that
    motivated the gate broke its central admission across two lines. A match is
    attributed to the line it STARTS on, so widening the window never
    double-counts.
    """
    for index, line in enumerate(lines):
        successor = lines[index + 1] if index + 1 < len(lines) else None
        joined = line.text if successor is None else f"{line.text} {successor.text.lstrip('# ')}"
        yield line, joined


def scan_file(path: Path, patterns: Iterable[MarkerPattern], *, repo_root: Path) -> list[Marker]:
    rel = path.relative_to(repo_root).as_posix()
    found: list[Marker] = []
    for line, window in _windows(file_prose(path)):
        for pattern in patterns:
            match = pattern.regex.search(window)
            if match is None or match.start() >= len(line.text):
                continue
            found.append(
                Marker(
                    path=rel,
                    lineno=line.lineno,
                    pattern_id=pattern.id,
                    remedy=pattern.remedy,
                    phrase=match.group(0),
                    text=window.strip()[:120],
                    issue_refs=issue_refs_near(window, match.span()),
                )
            )
            break
    return found


def applicable_patterns(source: str, patterns: Sequence[MarkerPattern]) -> tuple[MarkerPattern, ...]:
    """The families *source* could possibly match, decided without parsing it.

    Parsing the whole tree and running every family over every prose line costs
    an order of magnitude more than a substring pass over raw text, and most
    files can match no family at all. Each trigger is a literal that every
    alternative of its family must contain, so this can only ever admit too
    much -- never too little. ``TestTriggerPreFilter`` proves that against the
    real tree rather than by reading the regexes.
    """
    lowered = source.lower()
    return tuple(pattern for pattern in patterns if any(trigger in lowered for trigger in pattern.triggers))


def scan_tree(repo_root: Path, patterns: Iterable[MarkerPattern] | None = None) -> list[Marker]:
    resolved = tuple(patterns) if patterns is not None else load_marker_patterns()
    found: list[Marker] = []
    for path in scanned_files(repo_root):
        candidates = applicable_patterns(path.read_text(encoding="utf-8", errors="replace"), resolved)
        if candidates:
            found.extend(scan_file(path, candidates, repo_root=repo_root))
    return found


def per_file_counts(markers: Iterable[Marker]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for marker in markers:
        counts[marker.path] = counts.get(marker.path, 0) + 1
    return counts


@dataclasses.dataclass(frozen=True)
class IssueDeferral:
    marker: Marker
    issue: int

    def describe(self) -> str:
        return (
            f"  - {self.marker.path}:{self.marker.lineno} defers to #{self.issue} "
            f"via {self.marker.phrase!r}: {self.marker.text}"
        )


def issue_deferrals(markers: Iterable[Marker]) -> list[IssueDeferral]:
    """Every (marker, tracking issue) pair among *markers*."""
    return [IssueDeferral(marker=marker, issue=issue) for marker in markers for issue in marker.issue_refs]


@dataclasses.dataclass(frozen=True)
class DeferredMarkerBan:
    """The zero-tolerance half of the registry, over ``VERIFICATION_ROOTS``.

    Marker-form families only, and only where the marker OPENS a comment.
    Anchoring at the head of the comment is the whole discrimination: a marker
    that opens one instructs whoever reads the file next, while the same word
    inside a sentence is the file talking ABOUT markers -- which a guard's own
    tests and a scenario grading the anti-pattern must be free to do.
    """

    repo_root: Path
    patterns: tuple[MarkerPattern, ...]

    @classmethod
    def over(cls, repo_root: Path, patterns: Iterable[MarkerPattern] | None = None) -> "DeferredMarkerBan":
        resolved = tuple(patterns) if patterns is not None else load_marker_patterns()
        return cls(repo_root=repo_root, patterns=tuple(p for p in resolved if p.form is MarkerForm.MARKER))

    @property
    def scanned_files(self) -> list[Path]:
        found: list[Path] = []
        for rel in VERIFICATION_ROOTS:
            root_path = self.repo_root / rel
            if not root_path.is_dir():
                continue
            found.extend(
                path
                for path in sorted(root_path.rglob("*"))
                if path.suffix in COMMENT_SUFFIXES and "__pycache__" not in path.parts and path.is_file()
            )
        return found

    def scan_file(self, path: Path, patterns: Sequence[MarkerPattern] | None = None) -> list[Marker]:
        rel = path.relative_to(self.repo_root).as_posix()
        candidates = self.patterns if patterns is None else patterns
        found: list[Marker] = []
        for comment in file_comments(path):
            body = _COMMENT_OPENER_RE.sub("", comment.text, count=1)
            for pattern in candidates:
                match = pattern.regex.match(body)
                if match is None:
                    continue
                found.append(
                    Marker(
                        path=rel,
                        lineno=comment.lineno,
                        pattern_id=pattern.id,
                        remedy=pattern.remedy,
                        phrase=match.group(0),
                        text=body.strip()[:120],
                        issue_refs=issue_refs_near(body, match.span()),
                    )
                )
                break
        return found

    def markers(self) -> list[Marker]:
        found: list[Marker] = []
        for path in self.scanned_files:
            candidates = applicable_patterns(path.read_text(encoding="utf-8", errors="replace"), self.patterns)
            if candidates:
                found.extend(self.scan_file(path, candidates))
        return found

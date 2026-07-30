"""Scan the eval-harness surfaces for the bare cost claim "free".

A deterministic eval lane spends no model tokens, but it still spends CPU,
wall-clock and maintenance — so "free" is a false cost claim, and it was the
headline of a shipped skill. The accurate distinction the eval docs draw is
whether a lane calls a live model, which the repo already spells ``model-free``
(the ``X-free`` compound reads "without X", never "costs nothing").

The scan is deliberately narrow: only the eval-harness surfaces, and only the
STANDALONE token. A hyphenated compound (``model-free``, ``Django-free``,
``gap-free``, ``free-form``) is never a cost claim, so it is not a violation.
"""

import dataclasses
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

#: A bare ``free`` token in any casing — not one half of a hyphenated compound.
_BARE_FREE: Final[re.Pattern[str]] = re.compile(r"(?<![\w-])free(?![\w-])", re.IGNORECASE)

#: Eval-harness surfaces, relative to the repo root.
_SCANNED_FILES: Final[tuple[str, ...]] = (
    "skills/running-evals/SKILL.md",
    "docs/testing-skill-evals.md",
    "evals/README.md",
)
_SCANNED_DIRS: Final[tuple[str, ...]] = ("src/teatree/cli/eval",)


@dataclasses.dataclass(frozen=True)
class Violation:
    path: str
    line_number: int
    line: str

    def render(self) -> str:
        return f"{self.path}:{self.line_number}: {self.line.strip()}"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def scanned_paths(root: Path | None = None) -> list[Path]:
    base = root or repo_root()
    paths = [base / name for name in _SCANNED_FILES]
    for directory in _SCANNED_DIRS:
        paths.extend(sorted((base / directory).rglob("*.py")))
    return [path for path in paths if path.is_file()]


def scan_text(text: str, *, path: str) -> Iterator[Violation]:
    previous = ""
    for number, line in enumerate(text.splitlines(), start=1):
        match = _BARE_FREE.search(line)
        if match and not _is_wrapped_compound(previous, line, match) and not _is_absence_sense(line, match):
            yield Violation(path=path, line_number=number, line=line)
        previous = line


def _is_wrapped_compound(previous: str, line: str, match: re.Match[str]) -> bool:
    """True when the token is the tail of a compound wrapped across two lines.

    ``price-table-`` / ``free:`` is one hyphenated compound the line break split,
    not a bare cost claim.
    """
    return previous.rstrip().endswith("-") and not line[: match.start()].strip()


def _is_absence_sense(line: str, match: re.Match[str]) -> bool:
    """True for ``free of X`` — the spelled-out form of the ``X-free`` absence sense."""
    return line[match.end() :].startswith(" of ")


def scan(root: Path | None = None) -> list[Violation]:
    base = root or repo_root()
    violations: list[Violation] = []
    for path in scanned_paths(base):
        relative = path.relative_to(base).as_posix()
        violations.extend(scan_text(path.read_text(encoding="utf-8"), path=relative))
    return violations

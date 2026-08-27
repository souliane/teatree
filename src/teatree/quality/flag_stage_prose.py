"""Consumer prose must not describe a feature flag with a stage the registry denies.

``FEATURE_FLAGS`` carries each flag's stage, and a graduation edits one field there.
Every module that DESCRIBES the flag keeps its old wording, so the tree goes on calling a
shipped-ON flag dark long after it stopped being. That is not cosmetic: an agent reading
one such module concluded the flag does not ship ON and nearly built the wrong fix, and
one of the stale lines was a remediation string sending a human operator to turn on a
setting that was already on.

The scan is deliberately narrow, because a check that cannot fire is worth nothing and a
check that cries wolf gets suppressed. It asks ONE question of ONE word: a prose block
naming a flag and calling something ``dark`` must be naming a dark flag. Prose that says
"ships off" or "inert by default" without the word is not covered — a wider matcher would
need to read the sentence's subject, which no regex does.

The registry is the CALLER's to read (``teatree.quality`` sits below ``teatree.config``),
which also keeps the scan pure over its input: hand it the flags that are NOT dark, and it
never needs to know which stage name means dark. A ``DARK -> SETTLING`` graduation phrase
is scrubbed wherever it appears, since it states how a flag reached its current stage
rather than claiming it ships dark.
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.prose import ProseLine, file_prose

_DARK_WORD = re.compile(r"(?<![\w-])dark(?![\w-])", re.IGNORECASE)
_GRADUATION = re.compile(r"dark\s*-+>\s*\w+", re.IGNORECASE)
_EXCERPT_CHARS = 160


@dataclass(frozen=True)
class StaleStageClaim:
    """One prose block calling a flag dark that the registry stages otherwise."""

    path: Path
    lineno: int
    flag: str
    stage: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}  '{self.flag}' is {self.stage}, not dark  :: {self.excerpt}"


def _blocks(lines: list[ProseLine]) -> Iterator[list[ProseLine]]:
    """Runs of consecutive prose lines — one docstring or one comment block each."""
    run: list[ProseLine] = []
    for line in lines:
        if run and line.lineno != run[-1].lineno + 1:
            yield run
            run = []
        run.append(line)
    if run:
        yield run


def scan_file(path: Path, non_dark_flags: Mapping[str, str]) -> list[StaleStageClaim]:
    """Every claim in *path* against the flag-name -> stage map of the non-dark flags."""
    claims = []
    for block in _blocks(file_prose(path)):
        text = _GRADUATION.sub(" ", " ".join(line.text for line in block))
        if not _DARK_WORD.search(text):
            continue
        claims.extend(
            StaleStageClaim(path, block[0].lineno, flag, stage, text[:_EXCERPT_CHARS].strip())
            for flag, stage in non_dark_flags.items()
            if flag in text
        )
    return claims


def scan_tree(root: Path, non_dark_flags: Mapping[str, str], *, exclude: Path) -> list[StaleStageClaim]:
    """Every claim in the Python sources under *root*, skipping the declaring module."""
    return [
        claim for path in sorted(root.rglob("*.py")) if path != exclude for claim in scan_file(path, non_dark_flags)
    ]

"""Content-addressed ledger over the prose that describes a named anchor.

A lexical ban asks "does this sentence contain word W" and is silent on every
other sentence, so a rephrasing walks straight past it — measured three times on
souliane/teatree#4216, three residuals, one mechanism. A digest asks "is this the
sentence a human blessed" and is silent on none. It cannot tell TRUE from FALSE;
it cannot be BLIND, which is the property that failed.

SCOPE, stated as the operative limit rather than as coverage: a ``±radius``
window around a LITERAL occurrence of the anchor, in tracked files outside
``tests/`` that contain that literal. A test's prose is bound to the code by its
own execution; a doc's prose is bound to nothing, and every recorded residual was
on a doc surface.

KNOWN LIMITS — three shapes this instrument cannot see, and what does.

NO LITERAL AT ALL: a file that never spells the anchor is invisible, whatever the
radius. The fifth residual (#4381) was exactly this — the renderer that produced
the wrong deny text contains zero occurrences, so it was never a pegged surface.
The guard for that shape is
``tests/conformance/test_task_created_is_a_task_list_event.py``, which derives the
surface from the handlers the router REGISTERS and content-addresses their emitted
text, needing no literal.

OUTSIDE THE WINDOWS: prose elsewhere in a pegged file is invisible. Measured on
``hooks/CLAUDE.md``: 7 merged windows over 6193 of 76716 chars — 8.1% — and a
plant six characters before a window's start went unseen. Do not trust that
percentage as it ages; :func:`covered_bytes` is what the ``coverage`` pin and the
gate's failure message report, so the live number is always to hand.

UNCHANGED IS NOT TRUE: re-reading the window is the whole mechanism, and nothing
here evaluates the claim it carries.

REJECTED, so pass N+1 does not re-litigate them: pegging whole FILES or whole
SECTIONS. Neither covers limit 1 — the shape that actually fired — because both
still start from a literal occurrence, and a whole-file digest over a 563 KB doc
reds on every unrelated edit, which guarantees blind re-baselining.

Anchor-generic on purpose: the next retired premise costs one ledger table, not a
fourth bespoke ban.
"""

import dataclasses
import hashlib
import subprocess
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from tests._generated_artifacts import DURATIONS_CASSETTE

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LEDGER_TOML = Path(__file__).resolve().parent / "anchor_prose_pegs.toml"

#: TOML key inside an anchor's table that pins the window radius rather than a file.
_RADIUS_KEY = "radius"

#: TOML sub-table pinning covered BYTES per file. Reserved like the radius, so it is
#: never read as a peg list. Only the NUMERATOR is pinned: it moves in lockstep with
#: the digests (a window can only change size by changing content), so it costs no
#: churn beyond what already reds — whereas pinning the file TOTAL would red on every
#: unrelated edit to a 563 KB doc, the exact blind re-baselining this design refuses.
_COVERAGE_KEY = "coverage"

_RESERVED_KEYS = frozenset({_RADIUS_KEY, _COVERAGE_KEY})


def merged_windows(text: str, anchor: str, radius: int) -> list[str]:
    """Every ``±radius`` window around an *anchor* occurrence, overlaps merged.

    Merging is what makes a digest stable: two anchors closer than a window apart
    otherwise yield overlapping slices, so an edit between them reshuffles both
    digests and the ledger churns on prose it does not describe.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    while (i := text.find(anchor, start)) != -1:
        spans.append((max(0, i - radius), min(len(text), i + len(anchor) + radius)))
        start = i + 1

    merged: list[tuple[int, int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return [text[lo:hi] for lo, hi in merged]


def digest(window: str) -> str:
    """The ledger's content address for one window."""
    return hashlib.sha256(window.encode("utf-8")).hexdigest()[:16]


def window_digests(text: str, anchor: str, radius: int) -> list[tuple[str, str]]:
    """``(digest, window)`` per merged window, in file order."""
    return [(digest(w), w) for w in merged_windows(text, anchor, radius)]


def covered_bytes(text: str, anchor: str, radius: int) -> int:
    """How much of *text* any window actually covers — the ledger's real reach."""
    return sum(len(window) for window in merged_windows(text, anchor, radius))


def coverage_ratio(text: str, anchor: str, radius: int) -> str:
    """``<covered>/<total> = <pct>%`` for *text*, for a failure message to carry.

    The belief-correction has to land where a human is being asked to trust the
    instrument, which is the moment it fails — not in a doc they read once.
    """
    covered, total = covered_bytes(text, anchor, radius), len(text)
    pct = (covered / total * 100) if total else 0.0
    return f"{covered}/{total} = {pct:.1f}%"


def tracked_files(*, repo_root: Path = _REPO_ROOT) -> list[Path]:
    """Every tracked file, as absolute paths.

    Never ``check=True``: a git failure at COLLECTION time is an ERROR carrying no
    stderr, which reads as a broken harness rather than the failing invariant it is.
    """
    out = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 — repo-relative git, no user input
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, f"`git ls-files` failed in {repo_root}: {out.stderr.strip()}"
    return [p for p in (repo_root / line for line in out.stdout.splitlines() if line) if p.is_file()]


def doc_surface_files(anchor: str, *, repo_root: Path = _REPO_ROOT) -> list[Path]:
    """Tracked files mentioning *anchor*, minus ``tests/**`` and the durations cassette.

    The ledger's own two files live under ``tests/``, so this exclusion is also
    what stops the ledger digesting its own digests — there is no fixed point. The
    cassette is the same problem one step out: it spells the anchor only by recording
    the node ids of those test files, so it describes no surface a human could reword.
    """
    return sorted(
        path
        for path in tracked_files(repo_root=repo_root)
        if (rel := path.relative_to(repo_root).as_posix()) != DURATIONS_CASSETTE
        and not rel.startswith("tests/")
        and anchor in path.read_text(encoding="utf-8", errors="ignore")
    )


@dataclasses.dataclass(frozen=True)
class Ledger:
    """One anchor's pinned table: the radius, the per-file digests, the reach."""

    radius: int
    pegs: dict[str, tuple[str, ...]]
    coverage: dict[str, int]


def load_ledger(anchor: str, *, path: Path = _LEDGER_TOML) -> Ledger:
    """The pinned table for one *anchor*."""
    table = tomllib.loads(path.read_text(encoding="utf-8"))[anchor]
    radius = table[_RADIUS_KEY]
    assert isinstance(radius, int), f"[{anchor}] {_RADIUS_KEY} must be an int, got {radius!r}"
    return Ledger(
        radius=radius,
        pegs={key: tuple(value) for key, value in table.items() if key not in _RESERVED_KEYS},
        coverage=dict(table.get(_COVERAGE_KEY, {})),
    )


@dataclasses.dataclass(frozen=True)
class LedgerDrift:
    """Digests the tree carries but the ledger does not, and the reverse."""

    added: tuple[tuple[str, str, str], ...]
    dropped: tuple[tuple[str, str], ...]

    @property
    def ok(self) -> bool:
        return not self.added and not self.dropped

    def added_lines(self) -> list[str]:
        # The window text, not just the hash: a reviewer must read the new sentence
        # rather than pattern-match a digest they cannot evaluate.
        return [f'{path}: peg "{sha}" once you have read —\n{window}\n' for path, sha, window in self.added]

    def dropped_lines(self) -> list[str]:
        return [f'{path}: remove the peg "{sha}" — no window in the file produces it' for path, sha in self.dropped]


def diff_ledger(
    live: Mapping[str, Sequence[tuple[str, str]]],
    pegged: Mapping[str, Sequence[str]],
) -> LedgerDrift:
    """Compare live ``{file: [(digest, window)]}`` against the pegged digests."""
    added: list[tuple[str, str, str]] = []
    dropped: list[tuple[str, str]] = []
    for path in sorted(set(live) | set(pegged)):
        seen = Counter(sha for sha, _window in live.get(path, ()))
        want = Counter(pegged.get(path, ()))
        windows = dict(live.get(path, ()))
        added.extend((path, sha, windows[sha]) for sha in sorted((seen - want).elements()))
        dropped.extend((path, sha) for sha in sorted((want - seen).elements()))
    return LedgerDrift(added=tuple(added), dropped=tuple(dropped))

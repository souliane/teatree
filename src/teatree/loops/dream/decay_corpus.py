"""The memory CORPUS the decay phase reasons over — the files, and who cites whom.

Split out of :mod:`teatree.loops.dream.decay` because it answers a question the decay
POLICY does not: what memories exist in a dir, what each one is called, and which
documents point at which. The archival tiers consume that answer; they do not produce
it, and the retention guard, the inbound-link signal and the broken-citation warning
must all read the same one.

The load-bearing subtlety is IDENTITY. A memory names itself twice — the FILE is
``feedback_x_y.md`` while its frontmatter declares ``name: x-y`` (hyphenated, prefix
dropped) — and every citing body writes the filename form. Comparing the two forms
directly resolves to zero citations and reads a heavily-cited rule as orphaned, so
every alias is canonicalized UP to the filename before anything is counted.

DB-free and deterministic throughout: the caller passes an explicit ``memory_dir``, so
this is usable under ``SimpleTestCase``.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from teatree.loops.dream._shared import INDEX_NAME, NON_MEMORY_DOCS

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
#: Any ``name.md`` token — a ``](name.md)`` markdown link target, a backticked filename,
#: or a bare filename in a curated grouped index line. One regex covers all three because
#: they are the same citation wearing different punctuation.
_MEMORY_FILENAME_RE = re.compile(r"[\w.\-/]+\.md")
#: A line that is EXACTLY the ``- name.md`` pointer re-index writes for every memory.
#: Dropped before counting references: it regenerates wholesale, so it can never dangle
#: and it says nothing about which memories a reader actually leans on.
_GENERATED_POINTER_LINE_RE = re.compile(r"^\s*-\s+[\w.\-/]+\.md\s*$", re.MULTILINE)
_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)
#: A logical "lesson last-touched" frontmatter date — the age clock the budget tier
#: reads so a cross-link / re-index rewrite (which bumps ``st_mtime``) does NOT reset
#: the decay clock. Absent the field, the budget tier falls back to ``st_mtime``.
_LESSON_UPDATED_RE = re.compile(r"^lesson_updated:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class MemoryFile:
    path: Path
    name: str
    text: str
    mtime: datetime

    @property
    def lesson_touched(self) -> datetime:
        """The logical lesson last-touched time — frontmatter ``lesson_updated`` or mtime.

        The budget tier ages a lesson by WHEN IT WAS LAST MEANINGFULLY UPDATED, not
        when the file was last written: cross-link and re-index rewrite a file (and
        bump ``st_mtime``) without touching the lesson, so keying the decay clock on
        ``st_mtime`` would keep resetting it. The ``lesson_updated`` frontmatter date
        is that logical clock; absent it, ``st_mtime`` is the conservative fallback.
        """
        match = _LESSON_UPDATED_RE.search(self.text)
        if match:
            try:
                parsed = datetime.fromisoformat(match.group(1))
            except ValueError:
                return self.mtime
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        return self.mtime


def memory_name(path: Path, text: str) -> str:
    match = _FRONTMATTER_NAME_RE.search(text)
    return match.group(1) if match else path.stem


def load_memory_files(memory_dir: Path) -> list[MemoryFile]:
    files: list[MemoryFile] = []
    for md in sorted(memory_dir.glob("*.md")):
        if md.name in NON_MEMORY_DOCS:  # never load an index as a memory (#2723)
            continue
        try:
            text = md.read_text(encoding="utf-8")
            mtime = datetime.fromtimestamp(md.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        files.append(MemoryFile(path=md, name=memory_name(md, text), text=text, mtime=mtime))
    return files


def reference_tokens(text: str) -> set[str]:
    """Every token in *text* that may NAME another memory.

    ``[[wikilink]]`` targets plus every ``name.md`` token, once the lone generated
    pointer lines are dropped. What remains is a citation a reader would follow and an
    archival would break.
    """
    body = _GENERATED_POINTER_LINE_RE.sub("", text)
    return set(_WIKILINK_RE.findall(body)) | set(_MEMORY_FILENAME_RE.findall(body))


def reference_aliases(memory: MemoryFile) -> set[str]:
    """Every name *memory* answers to — its filename, its stem, and its frontmatter name."""
    return {memory.path.name, memory.path.stem, memory.name}


def canonical_by_alias(files: Sequence[MemoryFile]) -> dict[str, str]:
    """Resolve every unambiguous alias UP to the canonical filename that owns it.

    The FILENAME is a memory's identity. A memory declares a hyphenated, prefix-stripped
    frontmatter ``name`` while its citers write the filename form, so a citation has to be
    canonicalized rather than compared against whichever form the target happens to carry —
    comparing the two forms directly resolves to zero and reads a heavily-cited rule as
    orphaned. An alias two memories both claim is dropped: conflating distinct rules is
    worse than missing one citation.
    """
    owners: dict[str, set[str]] = {}
    for memory in files:
        for alias in reference_aliases(memory):
            owners.setdefault(alias, set()).add(memory.path.name)
    return {alias: next(iter(owner)) for alias, owner in owners.items() if len(owner) == 1}


def inbound_citers(files: Sequence[MemoryFile], index_text: str) -> dict[str, tuple[str, ...]]:
    """Map each memory's canonical filename to the documents citing it, sorted.

    Computed in ONE pass over the index and every body, so the reference guard, the
    inbound-link signal, and the broken-citation warning all read the same answer.
    """
    owner_of = canonical_by_alias(files)
    citers: dict[str, set[str]] = {}
    for source_name, text in [(INDEX_NAME, index_text)] + [(m.path.name, m.text) for m in files]:
        for token in reference_tokens(text):
            target = owner_of.get(token)
            if target is None or target == source_name:
                continue  # unresolvable, or a memory citing itself
            citers.setdefault(target, set()).add(source_name)
    return {target: tuple(sorted(names)) for target, names in citers.items()}


def is_referenced(memory: MemoryFile, citers: Mapping[str, tuple[str, ...]]) -> bool:
    """True iff a document OTHER than *memory* cites it."""
    return bool(citers.get(memory.path.name))


__all__ = [
    "MemoryFile",
    "canonical_by_alias",
    "inbound_citers",
    "is_referenced",
    "load_memory_files",
    "memory_name",
    "reference_tokens",
]

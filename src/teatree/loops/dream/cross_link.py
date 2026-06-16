"""Phase 4 of the dream pass — cross-link related memories (#1933 § 6).

After distillation, related memory files should point at each other so a reader
landing on one finds its siblings. The memory format already supports a
``[[name]]`` wiki-link; this phase adds those links DETERMINISTICALLY — two
memories are related when they share enough topic tokens (a Jaccard overlap over
their significant words above a floor), and a ``Related: [[a]] [[b]]`` line is
appended to each so the link is symmetric.

The phase is PURE and idempotent: it reads ``*.md`` under a given ``memory_dir``
(``MEMORY.md`` index excluded), computes the symmetric relation, and appends only
the links a file does not already carry — a re-run with the same memory set
rewrites byte-identically. It NEVER touches the user's real ``~/.claude``: the
caller passes an explicit ``memory_dir`` (the command resolves the real one; the
tests pass a tmp fixture), and a missing dir is a clean no-op.

Fault-isolated by construction: the function returns a typed summary and raises
only on a genuinely broken caller; the command runs it in a try/except so a
phase-4 failure never crashes the tick.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: A memory is significant enough to link when its token set overlaps a sibling's
#: by at least this Jaccard ratio. Tuned so two memories about the same subsystem
#: link while unrelated ones do not; deterministic, no model.
_RELATEDNESS_FLOOR = 0.18

#: Tokens shorter than this or in the stop set are noise for topic comparison.
_MIN_TOKEN_LEN = 4
_STOP_TOKENS = frozenset(
    {
        "this", "that", "with", "from", "have", "must", "never", "always", "when",
        "then", "than", "into", "onto", "over", "under", "before", "after", "they",
        "them", "their", "there", "here", "which", "while", "would", "could", "should",
        "binding", "memory", "memories", "rule", "rules", "note", "notes",
    }
)  # fmt: skip

_LINK_LINE_PREFIX = "Related:"
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class CrossLinkResult:
    """What one phase-4 pass did: files seen, link edges added, and whether dry."""

    files_seen: int
    links_added: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class _Memory:
    path: Path
    name: str
    tokens: frozenset[str]
    existing_links: frozenset[str]


def _memory_name(path: Path, text: str) -> str:
    match = _FRONTMATTER_NAME_RE.search(text)
    return match.group(1) if match else path.stem


def _topic_tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9_]+", text.lower())
    return frozenset(w for w in words if len(w) >= _MIN_TOKEN_LEN and w not in _STOP_TOKENS)


def _existing_links(text: str) -> frozenset[str]:
    return frozenset(_WIKILINK_RE.findall(text))


def _load_memories(memory_dir: Path) -> list[_Memory]:
    memories: list[_Memory] = []
    for md in sorted(memory_dir.glob("*.md")):
        if md.name == "MEMORY.md":
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        memories.append(
            _Memory(
                path=md,
                name=_memory_name(md, text),
                tokens=_topic_tokens(text),
                existing_links=_existing_links(text),
            )
        )
    return memories


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def compute_relations(memories: Sequence[_Memory]) -> dict[Path, set[str]]:
    """Map each memory's path to the set of sibling NAMES it should link.

    Symmetric: ``a`` relates to ``b`` iff their topic-token Jaccard is at or above
    the floor; both sides get each other's name. A memory never links itself.
    """
    relations: dict[Path, set[str]] = {m.path: set() for m in memories}
    for i, left in enumerate(memories):
        for right in memories[i + 1 :]:
            if left.name == right.name:
                continue
            if _jaccard(left.tokens, right.tokens) >= _RELATEDNESS_FLOOR:
                relations[left.path].add(right.name)
                relations[right.path].add(left.name)
    return relations


def _links_to_add(memory: _Memory, related_names: set[str]) -> list[str]:
    """The related names not already linked in the file — sorted, deduped."""
    return sorted(name for name in related_names if name not in memory.existing_links)


def _append_links(path: Path, names: Sequence[str]) -> None:
    rendered = " ".join(f"[[{name}]]" for name in names)
    text = path.read_text(encoding="utf-8")
    suffix = "" if text.endswith("\n") else "\n"
    path.write_text(f"{text}{suffix}{_LINK_LINE_PREFIX} {rendered}\n", encoding="utf-8")


def cross_link_memories(memory_dir: Path, *, dry_run: bool = False) -> CrossLinkResult:
    """Append symmetric ``[[name]]`` links between related memories under *memory_dir*.

    Idempotent: a file is only appended the links it does not already carry, so a
    re-run on an unchanged memory set adds zero. A missing dir is a clean no-op.
    Under *dry_run* the would-add count is computed but nothing is written.
    """
    if not memory_dir.is_dir():
        return CrossLinkResult(files_seen=0, links_added=0, dry_run=dry_run)
    memories = _load_memories(memory_dir)
    relations = compute_relations(memories)
    by_path: Mapping[Path, _Memory] = {m.path: m for m in memories}
    added = 0
    for path, related_names in relations.items():
        new_links = _links_to_add(by_path[path], related_names)
        if not new_links:
            continue
        added += len(new_links)
        if not dry_run:
            _append_links(path, new_links)
    return CrossLinkResult(files_seen=len(memories), links_added=added, dry_run=dry_run)


__all__ = ["CrossLinkResult", "compute_relations", "cross_link_memories"]

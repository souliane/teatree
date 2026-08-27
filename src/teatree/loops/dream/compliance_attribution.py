"""Which memory a user correction is a recurrence OF.

Attribution is best-match over distinctive tokens, never first-match: a single
incidental shared word (``worktree``, ``review``) pinned a recurrence on whichever
memory the scan reached first. Keeping the token model, the stopword list and the
match rule in one module means the thresholds are read beside the comparison they
bound rather than at the top of the file that merely uses them.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from teatree.loops.dream.replay import ConsolidationExtract, WeightedSnippet
from teatree.loops.dream.transcript_extract import looks_like_user_correction

#: Tokens shorter than this carry no topical signal — a correction sharing only
#: "the"/"not" with a memory is not a recurrence of that memory's rule.
_MIN_TOKEN_LEN = 5

#: A correction must share at least this many DISTINCTIVE tokens with a memory to be
#: attributed as a recurrence of that memory's rule (F6.5). A single incidental shared
#: token ("worktree", "review") is noise that misattributes the recurrence to an
#: arbitrary memory; requiring two topical tokens keeps the attribution honest.
_MIN_SHARED_TOKENS = 2

#: Words that are frequent in BOTH memory bodies and correction prose and so are
#: non-discriminating — they must not, on their own, match a correction to a memory.
_STOPWORDS = frozenset(
    {
        "again",
        "always",
        "never",
        "should",
        "would",
        "their",
        "there",
        "these",
        "those",
        "which",
        "while",
        "about",
        "instruction",
        "instructions",
        "follow",
        "memory",
        "rule",
        "feedback",
        "binding",
    }
)

_WORD_RE = re.compile(r"[a-z][a-z0-9_]+")
_NAME_LINE_RE = re.compile(r"^name:\s*(?P<slug>[\w\-]+)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class _MemoryRule:
    slug: str
    tokens: frozenset[str]


def _significant_tokens(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall(text.lower()) if len(word) >= _MIN_TOKEN_LEN and word not in _STOPWORDS}


def _memory_slug(snippet: WeightedSnippet) -> str:
    match = _NAME_LINE_RE.search(snippet.text)
    if match:
        return match.group("slug")
    return snippet.path.stem


def _memory_rules(extract: ConsolidationExtract) -> list[_MemoryRule]:
    """Every memory available this pass as a (slug, distinctive-tokens) rule, indexed by slug.

    A COMPLETE index over the pass's memory members keyed by slug (F6.5), not a
    first-wins sample: a memory whose snippet recurs (or whose slug collides) unions
    its distinctive tokens onto ONE rule rather than spawning two half-populated rules
    the attribution could pick between arbitrarily.
    """
    by_slug: dict[str, set[str]] = {}
    for snippet in extract.snippets:
        if snippet.kind != "memory":
            continue
        slug = _memory_slug(snippet)
        tokens = _significant_tokens(snippet.text) | _significant_tokens(slug)
        by_slug.setdefault(slug, set()).update(tokens)
    return [_MemoryRule(slug=slug, tokens=frozenset(tokens)) for slug, tokens in by_slug.items()]


def _correction_lines(extract: ConsolidationExtract) -> list[str]:
    """Every user-correction line across the transcript snippets (LLM-free ground truth)."""
    lines: list[str] = []
    for snippet in extract.snippets:
        if snippet.kind == "memory":
            continue
        lines.extend(line for line in snippet.text.splitlines() if looks_like_user_correction(line))
    return lines


def _directive_identity(line: str) -> str:
    """A stable identity for an in-session directive violation with no backing memory."""
    tokens = sorted(_significant_tokens(line))[:4]
    return "-".join(tokens) if tokens else "in-session-directive"


def _backing_memory(line: str, memory_rules: Sequence[_MemoryRule]) -> _MemoryRule | None:
    """The memory rule the correction line most overlaps, or ``None`` (F6.5).

    Attribution is BEST-match, not first-match, and requires at least
    :data:`_MIN_SHARED_TOKENS` distinctive shared tokens: the rule with the most
    shared tokens wins, ties broken by the higher Jaccard (so a small, tightly-matching
    memory beats a large one that merely happens to share the same count). A single
    incidental shared token no longer attributes a recurrence to an arbitrary memory —
    the misattribution the old first-token-wins scan produced.
    """
    line_tokens = _significant_tokens(line)
    if not line_tokens:
        return None
    best: _MemoryRule | None = None
    best_key = (0, 0.0)
    for rule in memory_rules:
        shared = rule.tokens & line_tokens
        if len(shared) < _MIN_SHARED_TOKENS:
            continue
        union = rule.tokens | line_tokens
        key = (len(shared), len(shared) / len(union) if union else 0.0)
        if key > best_key:
            best, best_key = rule, key
    return best

"""Cold-tier memory RECALL — surface an archived rule when the prompt is relevant (#2746).

PR1 (#2723) moved the lowest-signal memories — including ~130 BINDING / Non-Negotiable
rules of the ~693-entry corpus — out of the session-loaded hot ``MEMORY.md`` into a
COLD tier: the full restorable bodies under ``archive/`` plus a one-line-per-entry,
NOT-session-loaded ``MEMORY_ARCHIVE.md`` cold index. Those cold rules stop influencing
behaviour. RECALL closes that gap: when the user's prompt is topically relevant to a
cold rule, this pure core scores the cold index against the prompt and returns the top
hits so a thin hook can inject them for that one turn.

The scoring is deterministic and reads ONLY the cold index (``MEMORY_ARCHIVE.md``) and
the hot index (``MEMORY.md``) — never the ~540 archived bodies, because the cold-index
line carries the lesson signature (stronger after #2746 nit-4). DB-free and stdlib-only
at the top level (it imports two sibling-module FILENAME constants, both DB-free) so the
``UserPromptSubmit`` hook can import it without ``django.setup()``.

Relevance floor: a hit needs at least :data:`RECALL_MIN_TOKEN_MATCHES` distinct token
matches (name + signature) before any BINDING / user boost applies, so an irrelevant
BINDING rule is never surfaced on a single incidental token. A cold hit already present
in the hot index (by pointer name OR by its signature being a substring of the hot text)
is dropped — recall never echoes a rule the session already loaded.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# The cold/hot index FILENAMES are the stable cross-phase contract: the decay
# (archive) phase WRITES ``MEMORY_ARCHIVE.md`` and the reindex phase WRITES
# ``MEMORY.md``. Reference those constants directly so a rename in one place can
# never silently desync the recall reader. Both modules are DB-free at import
# (stdlib only), so importing them keeps this core hook-importable without Django.
from teatree.loops.dream.decay import _ARCHIVE_INDEX_NAME as COLD_INDEX_NAME
from teatree.loops.dream.reindex import _INDEX_NAME as HOT_INDEX_NAME

#: Max hits surfaced per turn.
RECALL_LIMIT = 5
#: Max bytes of the rendered recall block (header + lines).
RECALL_MAX_BYTES = 1500
#: Distinct token matches a cold entry needs before it is even a candidate (the
#: relevance floor — never surface a rule on a single incidental token overlap).
RECALL_MIN_TOKEN_MATCHES = 2
#: Per-injected-line character cap (a long cold signature is clipped on inject).
RECALL_INJECT_LINE_MAX = 200

_RECALL_HEADER = "Relevant archived memory rules (cold tier — full bodies under archive/, Read for detail):"

#: Ported from ``hook_router._AMBIENT_CONTEXT_RE`` into the pure core so injected
#: MEMORY.md / CLAUDE.md text the harness wraps in ``<system-reminder>`` /
#: ``<command-*>`` blocks can never self-match against the cold index. An
#: unterminated opening wrapper (a truncated injection) is dropped tag→EOS too.
_AMBIENT_CONTEXT_RE = re.compile(
    r"<(system-reminder|command-message|command-name|command-args|local-command-stdout)\b[^>]*>"
    r".*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_AMBIENT_OPEN_RE = re.compile(
    r"<(system-reminder|command-message|command-name|command-args|local-command-stdout)\b[^>]*>.*",
    re.DOTALL | re.IGNORECASE,
)
#: Bound the ambient-strip / tokenize cost on a huge pasted prompt (mirrors the
#: 64 KiB cap the hook applies before its own DOTALL regexes).
_QUERY_MAX_CHARS = 65536

_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_MIN_TOKEN_LEN = 3
#: One cold-index body line: ``- <name>.md — <signature>`` (em-dash separator,
#: matching ``decay._cold_index_line``). The signature group is optional.
_COLD_LINE_RE = re.compile(r"^-\s+(\S+\.md)(?:\s+—\s+(.*))?$")
#: The leading filename pointer of a hot ``MEMORY.md`` line (mirrors
#: ``gates._MEMORY_REF_RE`` — bare or legacy ``[name.md](name.md)`` form).
_HOT_NAME_RE = re.compile(r"^\s*-\s+\[?([\w.\-/]+\.md)\b")
_BINDING_MARKERS = ("binding", "non-negotiable")

#: Per-match additive boosts, applied ONLY once a hit clears the relevance floor.
_BINDING_BOOST = 3
_USER_BOOST = 2

#: Dedup-prefix length for the renamed-file guard. The hot index used to clip a
#: per-line summary to 45 chars (then rstrip one trailing space) before it became a
#: bare-pointer index, so a long cold signature is never a verbatim substring of hot
#: text carried over from that era (curated ``[Title](name.md) — summary`` lines are
#: still legitimate in the hot index) — compare a prefix bounded just under that clip
#: so a clipped, renamed rule is still recognised as already hot.
_DEDUP_PREFIX_CHARS = 43

#: A small high-frequency stopword set dropped from query tokens so common words
#: never inflate a match. Sub-3-char tokens are dropped separately.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "any",
        "can",
        "was",
        "our",
        "out",
        "use",
        "with",
        "this",
        "that",
        "from",
        "they",
        "them",
        "then",
        "than",
        "have",
        "has",
        "had",
        "his",
        "her",
        "its",
        "into",
        "what",
        "when",
        "your",
        "why",
        "who",
        "which",
        "will",
        "would",
        "should",
        "could",
        "about",
        "after",
        "before",
        "where",
        "while",
        "their",
        "there",
        "these",
        "those",
        "been",
        "were",
        "such",
        "only",
        "also",
        "must",
        "never",
        "always",
        "via",
        "per",
        "etc",
    }
)


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One cold-tier rule worth surfacing for the current turn.

    ``name`` is the ``<name>.md`` pointer; ``signature`` is the cold-index
    signature line (the real lesson after #2746 nit-4); ``score`` is the
    relevance score (higher first); ``binding`` flags a BINDING / Non-Negotiable
    rule (it sorts ahead of a plain rule at equal score).
    """

    name: str
    signature: str
    score: int
    binding: bool


def _strip_ambient(prompt: str) -> str:
    """Drop harness-injected ``<system-reminder>`` / ``<command-*>`` blocks from *prompt*."""
    capped = prompt[:_QUERY_MAX_CHARS]
    stripped = _AMBIENT_CONTEXT_RE.sub(" ", capped)
    # An UNTERMINATED open tag drops from the tag to end-of-string — so an unterminated
    # <system-reminder> mention in a genuine prompt truncates the rest of the query.
    # Intentional, mirrors hook_router._strip_ambient_context: leaked ambient text must
    # never reach the matcher, and genuine intent sits early (the harness appends blocks).
    return _AMBIENT_OPEN_RE.sub(" ", stripped)


def _tokens(text: str) -> set[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords + sub-3-char tokens."""
    return {tok for tok in _TOKEN_RE.split(text.lower()) if len(tok) >= _MIN_TOKEN_LEN and tok not in _STOPWORDS}


def _clean_query_tokens(prompt: str) -> set[str]:
    """The relevance token set for *prompt* — ambient blocks stripped first.

    Stripping the harness wrappers FIRST is load-bearing: a ``<system-reminder>``
    carrying the MEMORY.md / CLAUDE.md dump must not let injected text self-match
    the cold index and surface unrelated rules every turn (#2746).
    """
    return _tokens(_strip_ambient(prompt))


def _parse_cold_index(text: str) -> list[tuple[str, str]]:
    """Parse ``MEMORY_ARCHIVE.md`` body lines into ``(name, signature)`` pairs.

    Only ``- <name>.md — <signature>`` lines match; the header / preamble lines
    (which never lead with ``- ``) are skipped. A line with no signature yields
    an empty-string signature.
    """
    entries: list[tuple[str, str]] = []
    for raw in text.splitlines():
        match = _COLD_LINE_RE.match(raw.strip())
        if match:
            entries.append((match.group(1), (match.group(2) or "").strip()))
    return entries


def _hot_pointer_names(hot_text: str) -> set[str]:
    """The set of ``<name>.md`` pointers the hot ``MEMORY.md`` lists (line-leading)."""
    return {match.group(1) for raw in hot_text.splitlines() if (match := _HOT_NAME_RE.match(raw.strip()))}


def _is_binding(signature: str) -> bool:
    lowered = signature.lower()
    return any(marker in lowered for marker in _BINDING_MARKERS)


def _score(query_tokens: set[str], name: str, signature: str) -> tuple[int, bool]:
    """Score one cold entry against *query_tokens*; return ``(score, binding)``.

    The relevance FLOOR counts DISTINCT matched tokens across the entry (name and
    signature): a hit needs at least :data:`RECALL_MIN_TOKEN_MATCHES` distinct tokens,
    so a single incidental token — even one that happens to be a filename token — never
    surfaces a rule. Below the floor the score is ``0`` (dropped). Only POST-floor does a
    NAME-token match add weight: ``+1`` per matched name token (the filename is a curated
    topic identifier, so it double-weights for RANKING), then ``+3`` for a BINDING /
    Non-Negotiable signature and ``+2`` for a ``user_*`` name. The name weight and the
    boosts affect ranking only — never whether an entry clears the floor.
    """
    name_tokens = _tokens(name)
    entry_tokens = name_tokens | _tokens(signature)
    binding = _is_binding(signature)
    distinct = len(query_tokens & entry_tokens)
    if distinct < RECALL_MIN_TOKEN_MATCHES:
        return 0, binding
    score = distinct + len(query_tokens & name_tokens)
    if binding:
        score += _BINDING_BOOST
    if name.lower().startswith("user_"):
        score += _USER_BOOST
    return score, binding


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _render_hit_line(hit: RecallHit) -> str:
    """Render one hit as ``- <name>.md — <signature>``, clipped to the per-line cap."""
    line = f"- {hit.name} — {hit.signature}" if hit.signature else f"- {hit.name}"
    if len(line) > RECALL_INJECT_LINE_MAX:
        line = line[: RECALL_INJECT_LINE_MAX - 1].rstrip() + "…"
    return line


def render_recall_block(hits: Sequence[RecallHit]) -> str:
    """Render the recall block: a 1-line header + one line per hit, or ``""`` when empty."""
    if not hits:
        return ""
    return _RECALL_HEADER + "\n" + "\n".join(_render_hit_line(hit) for hit in hits)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeDecodeError):
        # A corrupt / non-UTF-8 index honors the "returns '' on error" contract —
        # UnicodeDecodeError is a ValueError, NOT an OSError, so catch it explicitly.
        return ""


def _signature_in_hot(normalized_sig: str, hot_norm: str) -> bool:
    """Whether the cold *normalized_sig* is already present in the hot index text.

    The renamed-file guard: a rule moved to a DIFFERENT hot filename. The hot index
    clips its summary, so a long cold signature is never a verbatim substring of the
    hot text — compare a :data:`_DEDUP_PREFIX_CHARS` prefix (bounded just under the hot
    clip) so a clipped, renamed rule is still recognised as already loaded. A short
    signature compares in full. Empty signatures never match (they would match every
    hot text).
    """
    if not normalized_sig:
        return False
    return normalized_sig[:_DEDUP_PREFIX_CHARS] in hot_norm


def recall_cold_memory(
    memory_dir: Path,
    query: str,
    *,
    limit: int = RECALL_LIMIT,
    max_bytes: int = RECALL_MAX_BYTES,
) -> list[RecallHit]:
    """Return the cold-tier rules most relevant to *query*, ranked, deduped, capped.

    Reads ``MEMORY_ARCHIVE.md`` once and ``MEMORY.md`` once under *memory_dir* — a
    missing cold index or an empty / signal-free query yields ``[]`` (silent
    degrade). Each cold entry is scored (:func:`_score`); entries below the
    relevance floor are dropped. A cold hit already in the hot index is dropped
    two ways: (a) its pointer name is listed in ``MEMORY.md``, or (b) its
    normalized signature is a substring of the normalized hot text (the
    renamed-file guard) — recall never echoes a session-loaded rule. The survivors
    are sorted by ``(score desc, binding desc, name asc)``, the top *limit* taken,
    then greedily accumulated so the rendered block stays within *max_bytes*.
    """
    cold_text = _read_text(memory_dir / COLD_INDEX_NAME)
    if not cold_text:
        return []
    query_tokens = _clean_query_tokens(query)
    if not query_tokens:
        return []

    hot_text = _read_text(memory_dir / HOT_INDEX_NAME)
    hot_names = _hot_pointer_names(hot_text)
    hot_norm = _normalize(hot_text)

    candidates: list[RecallHit] = []
    for name, signature in _parse_cold_index(cold_text):
        if name in hot_names:
            continue  # already in the hot index (by pointer)
        score, binding = _score(query_tokens, name, signature)
        if score == 0:
            continue  # below the relevance floor
        if _signature_in_hot(_normalize(signature), hot_norm):
            continue  # renamed-file guard: the lesson already lives in the hot index
        candidates.append(RecallHit(name=name, signature=signature, score=score, binding=binding))

    candidates.sort(key=lambda hit: (-hit.score, not hit.binding, hit.name))
    return _within_byte_budget(candidates[:limit], max_bytes)


def _within_byte_budget(hits: Sequence[RecallHit], max_bytes: int) -> list[RecallHit]:
    """Greedily keep the leading hits whose rendered block stays within *max_bytes*."""
    selected: list[RecallHit] = []
    for hit in hits:
        if len(render_recall_block([*selected, hit]).encode("utf-8")) > max_bytes:
            break
        selected.append(hit)
    return selected


__all__ = [
    "COLD_INDEX_NAME",
    "HOT_INDEX_NAME",
    "RECALL_INJECT_LINE_MAX",
    "RECALL_LIMIT",
    "RECALL_MAX_BYTES",
    "RECALL_MIN_TOKEN_MATCHES",
    "RecallHit",
    "recall_cold_memory",
    "render_recall_block",
]

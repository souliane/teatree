"""Structured EMIT records for items the CLI did NOT auto-delete (#2763).

The architecture seam: the CLI auto-deletes ONLY provably-redundant items; every
other item (unique content, banned-terms, uncertain, live, colleague-owned) it
EMITS as a machine-readable record for the separate judgment SKILL to route
(superseded / relevant / salvage-to-fresh-PR). The CLI never makes the subjective
keep-forever call — "not-proven-redundant → emit", the terminal state is the
skill, not silent retention.

JSON SCHEMA (one object per emitted item; the skill consumes a JSON array of
these). ``schema_version`` is bumped on any breaking field change.

```jsonc
{
    "schema_version": 1,
    "path": "/abs/path/to/worktree-or-clone",       // on-disk location, "" for a bare branch/stash
    "branch": "feat-x",                             // the branch (or "stash@{N}" for a stash)
    "kind": "worktree",                             // "worktree" | "branch" | "stash"
    "unique_commit_shas": ["<sha>", ...],           // commits whose CONTENT is not provably on target
    "merged_with_post_merge_work": true,            // forge-merged BUT current tip has unique content
    "banned_terms_status": "contains",              // "clean" | "contains" | "unknown"
    "banned_terms_found": ["credential", ...],      // distinct banned terms hit (empty when clean)
    "liveness": "",                                 // "" when not live, else the keep-reason phrase
    "last_commit_date": "2026-06-27T10:00:00+00:00",// ISO-8601 of the tip commit, "" if unknown
    "owner": "souliane"                             // resolved tip author identity, "" if unknown
}
```

The skill reads ``unique_commit_shas`` + ``merged_with_post_merge_work`` to decide
salvage-to-fresh-PR, ``banned_terms_status`` to know whether to clean before
salvage, and ``liveness`` to defer a live item.
"""

import re
from dataclasses import dataclass, field
from typing import TypedDict

EMIT_SCHEMA_VERSION = 1


class EmitRecordDict(TypedDict):
    """The JSON shape of one emitted record (see the module docstring for field semantics)."""

    schema_version: int
    path: str
    branch: str
    kind: str
    unique_commit_shas: list[str]
    merged_with_post_merge_work: bool
    banned_terms_status: str
    banned_terms_found: list[str]
    liveness: str
    last_commit_date: str
    owner: str


# High-signal banned terms for a public-repo leak / internal-identifier scan. The
# skill does the FINAL judgment; this is the advisory pre-flag so it knows which
# items need a banned-terms clean before salvage. Kept high-signal on purpose —
# common English words (email/phone/key) are excluded to avoid false positives.
_BANNED_TERM_PATTERNS: tuple[str, ...] = (
    r"\bleak(?:ed|s)?\b",
    r"\bredact(?:ed|s)?\b",
    r"\bcredentials?\b",
    r"\bsecret\b",
    r"\bpassword\b",
    r"\bssn\b",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"/Users/[A-Za-z0-9._-]+",
    r"/home/[A-Za-z0-9._-]+",
    r"\bxox[bp]-[A-Za-z0-9-]+",
)
_BANNED_RE = re.compile("|".join(_BANNED_TERM_PATTERNS), re.IGNORECASE)


def scan_banned_terms(text: str) -> list[str]:
    """Return the distinct banned-term hits in ``text`` (lower-cased), in first-seen order."""
    seen: dict[str, None] = {}
    for match in _BANNED_RE.findall(text):
        token = (match if isinstance(match, str) else next(m for m in match if m)).strip().lower()
        if token:
            seen.setdefault(token, None)
    return list(seen)


def banned_terms_status(texts: list[str]) -> tuple[str, list[str]]:
    """Classify a set of texts (commit messages, diffs): ``(status, found)``.

    ``("unknown", [])`` when there is nothing to scan (the content was
    unreadable), ``("clean", [])`` when scanned and no hit, ``("contains",
    [...])`` when a banned term appears — so the skill cleans before salvage.
    """
    scannable = [t for t in texts if t.strip()]
    if not scannable:
        return "unknown", []
    found: list[str] = []
    for text in scannable:
        for token in scan_banned_terms(text):
            if token not in found:
                found.append(token)
    return ("contains", found) if found else ("clean", [])


@dataclass(frozen=True, slots=True)
class CleanupEmitRecord:
    """One machine-readable record for an item the CLI did NOT auto-delete.

    See the module docstring for the JSON schema. :meth:`to_dict` is the single
    serialization point (the skill consumes a JSON array of these).
    """

    path: str
    branch: str
    kind: str
    unique_commit_shas: list[str] = field(default_factory=list)
    merged_with_post_merge_work: bool = False
    banned_terms_status: str = "unknown"
    banned_terms_found: list[str] = field(default_factory=list)
    liveness: str = ""
    last_commit_date: str = ""
    owner: str = ""

    def to_dict(self) -> EmitRecordDict:
        """JSON-serializable record including the schema version."""
        return {
            "schema_version": EMIT_SCHEMA_VERSION,
            "path": self.path,
            "branch": self.branch,
            "kind": self.kind,
            "unique_commit_shas": list(self.unique_commit_shas),
            "merged_with_post_merge_work": self.merged_with_post_merge_work,
            "banned_terms_status": self.banned_terms_status,
            "banned_terms_found": list(self.banned_terms_found),
            "liveness": self.liveness,
            "last_commit_date": self.last_commit_date,
            "owner": self.owner,
        }

"""Fail-closed evidence gate for outcome-claiming task completions (#1280).

The recurrence this forecloses: a task was repeatedly marked completed with
a free-text note that ASSERTED an external outcome — "merged via X",
"posted the review", "shipped !1234", "deployed to dev" — but carried NO
resolvable pointer to the artifact that would let anyone confirm the claim.
The phantom-completion then surfaces later as a "done-but-not-done" bug.

A remembered rule ("done claims require artifact evidence") did not hold under
load; this module is the deterministic substitute, mirroring the
:mod:`teatree.core.dod_gate` shape: a pure function over the completion note,
a dedicated error subclass, and a clear message naming exactly what is missing.

Scope is deliberately narrow — it is NON-breaking by construction:

Outcome claim
    A completion note whose text asserts an EXTERNAL outcome (one of
    :data:`OUTCOME_CLAIM_KINDS` — merged / posted / shipped / deployed — or a
    close synonym in :data:`_CLAIM_SYNONYMS`). Only these require evidence.

Ordinary completion
    A completion with NO note, or a note that records internal progress with
    no external-outcome verb, is untouched — it never needs a pointer. This is
    the common ``tasks complete`` / internal FSM path and must keep working.

Resolvable pointer
    The note must also contain something an auditor could follow: a URL, a
    git SHA (7-40 hex), an MR/PR/issue reference (``!123`` / ``#123``), a
    forge note id (``note_xxx``), or a filesystem path. The shape check is
    intentionally minimal — presence of one resolvable token, not a strict
    grammar — so a genuine claim is never blocked on formatting.

The gate is invoked from the single out-of-band completion surface
(``tasks complete --note``) — the place an agent records "this work landed
externally". On a block it raises :class:`CompletionEvidenceError`; the task
is NOT marked completed.
"""

import re
from dataclasses import dataclass

from teatree.core.models.errors import InvalidTransitionError

# The external-outcome claim kinds that REQUIRE a resolvable artifact pointer.
# Matched against the note's verbs (see ``_CLAIM_SYNONYMS``); a note with none
# of these is an ordinary internal completion and needs no evidence.
OUTCOME_CLAIM_KINDS = frozenset({"merged", "posted", "shipped", "deployed"})

# Surface synonyms that map onto an outcome claim kind. "landed" and "released"
# are the spellings the agent actually uses in completion notes for the same
# external facts the canonical kinds name.
_CLAIM_SYNONYMS: dict[str, str] = {
    "merged": "merged",
    "merge": "merged",
    "landed": "merged",
    "posted": "posted",
    "post": "posted",
    "published": "posted",
    "shipped": "shipped",
    "ship": "shipped",
    "released": "shipped",
    "release": "shipped",
    "deployed": "deployed",
    "deploy": "deployed",
}

# Longest-first so a verb that is a prefix of another (``merge`` vs ``merged``)
# does not preempt the more specific spelling.
_CLAIM_VERBS: list[str] = sorted(_CLAIM_SYNONYMS, key=lambda verb: -len(verb))
_CLAIM_VERB_RE = re.compile(r"\b(" + "|".join(_CLAIM_VERBS) + r")\b", re.IGNORECASE)

# A resolvable pointer is any ONE of: a URL, a git SHA (7-40 hex), an MR/PR/
# issue reference (``!123`` / ``#123``), a forge note id, or a filesystem path.
# Minimal-by-design: presence of a token an auditor could follow, not a strict
# grammar — a genuine claim must never be blocked on formatting.
_POINTER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://\S+"),  # URL
    re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE),  # git SHA
    re.compile(r"[!#]\d+"),  # MR / PR / issue reference
    re.compile(r"\bnote_[A-Za-z0-9]+\b"),  # forge note id
    re.compile(r"(?:^|\s)/?(?:[\w.-]+/)+[\w.-]+"),  # filesystem path
)


class CompletionEvidenceError(InvalidTransitionError):
    """A completion asserting an external outcome carried no resolvable pointer.

    A subclass of :class:`InvalidTransitionError` (sibling of
    ``DirtyWorktreeError`` / ``DodLocalE2EError``) so a caller inside the
    Task completion transaction sees the same refusal class and the FSM stays
    put. The message names the missing piece (the claim kind plus the kinds of
    pointer that would satisfy it) so the agent can supply it without guessing.
    """


@dataclass(frozen=True)
class CompletionEvidence:
    """Evidence backing an outcome-claiming completion.

    ``claim_kind`` is one of :data:`OUTCOME_CLAIM_KINDS` (or empty for an
    ordinary internal completion); ``artifact_pointer`` is the resolvable
    token an auditor follows; ``fresh_observation`` is an optional free-text
    note of what was observed when the claim was made.
    """

    claim_kind: str
    artifact_pointer: str
    fresh_observation: str = ""

    @property
    def asserts_outcome(self) -> bool:
        return self.claim_kind in OUTCOME_CLAIM_KINDS

    @property
    def has_resolvable_pointer(self) -> bool:
        return has_resolvable_pointer(self.artifact_pointer)


def detect_claim_kind(note: str) -> str:
    """Return the outcome claim kind a completion note asserts, or ``""``.

    Empty means the note records no external outcome — an ordinary internal
    completion that needs no evidence. The FIRST matching verb wins, mapped
    through ``_CLAIM_SYNONYMS`` to its canonical kind.
    """
    match = _CLAIM_VERB_RE.search(note or "")
    if match is None:
        return ""
    return _CLAIM_SYNONYMS[match.group(1).lower()]


def has_resolvable_pointer(note: str) -> bool:
    """True iff *note* contains at least one resolvable artifact pointer."""
    text = note or ""
    return any(pattern.search(text) for pattern in _POINTER_RES)


def evidence_from_note(note: str) -> CompletionEvidence:
    """Parse a free-text completion note into a :class:`CompletionEvidence`.

    The note itself IS the evidence carrier on the ``tasks complete --note``
    surface: the claim kind comes from its verbs, the artifact pointer is the
    whole note (any resolvable token in it satisfies the gate). An empty note
    yields an empty, non-asserting evidence value.
    """
    return CompletionEvidence(claim_kind=detect_claim_kind(note), artifact_pointer=note or "")


def check_completion_evidence(note: str) -> None:
    """Refuse an outcome-claiming completion that carries no resolvable pointer.

    Fail-closed: a note asserting an external outcome (merged / posted /
    shipped / deployed) MUST contain a resolvable pointer. A note with no
    outcome claim — or no note at all — passes untouched, so ordinary internal
    completions are never gated.
    """
    evidence = evidence_from_note(note)
    if not evidence.asserts_outcome:
        return
    if evidence.has_resolvable_pointer:
        return
    msg = (
        f"Refusing to complete: the note asserts an external outcome "
        f"({evidence.claim_kind!r}) but carries no resolvable artifact pointer. "
        f"Add a pointer the claim can be verified against — a URL, a git SHA, "
        f"an MR/PR/issue reference (!123 / #123), a forge note id, or a file "
        f"path — e.g. `--note '{evidence.claim_kind} via <url-or-!id-or-sha>'`."
    )
    raise CompletionEvidenceError(msg)

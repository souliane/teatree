"""Pure text predicates over a deferred question's own wording.

Neither needs the ORM: one collapses cosmetically-different clones of the same
question onto one dedupe marker, the other decides whether a ``needs_user_input``
reason is the agent reporting its OWN mis-provisioning rather than asking the owner
anything. They live beside the rest of the ``modelkit`` domain vocabulary so the
model module stays the persistence concern alone.
"""

import hashlib
import re

_WHITESPACE_RE = re.compile(r"\s+")


def question_fingerprint(text: str) -> str:
    """A normalized-text fingerprint that collapses cosmetically-different clones.

    Lowercases, strips, and collapses runs of whitespace before hashing, so eight
    "I lack the tools to review" review-failure clones — differing only in
    trailing whitespace or casing — map to one marker and dedup to a single
    :class:`DeferredQuestion` instead of eight identical rows.
    """
    normalized = _WHITESPACE_RE.sub(" ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


#: Signals that a ``needs_user_input`` reason is a tool-lack / mis-provisioned
#: DISPATCH fault — a session reporting it lacks the tools / checkout / access to do
#: its assigned work — not a genuine decision the owner must make. Any one branch is
#: sufficient. Each keys on a signal owner *decision* questions do not carry, so a
#: real "how should I proceed on X?" ("cannot decide", "no clean approach") stays
#: OWNER_QUESTION. Branches (4)-(7) were added after (1)-(3) still leaked review
#: parks that reported the same fault by its consequence/symptom (#201/#202).
_TOOL_LACK_SELFREPORT_RE = re.compile(
    r"(?:"
    # (1) capability negation adjacent to a tool word
    r"\b(?:lack|lacks|lacking|no|without|missing|denied|deprived of)\b[^.]{0,40}?"
    r"\b(?:shell|bash|gh|tool|tools|toolset)\b"
    r"|\bshell[- ]?denied\b"  # (2) bare "shell-denied"
    r"|\bneeds?\b[^.]{0,40}?\bsession\b[^.]{0,40}?\btool"  # (3) hand-off phrasings
    r"|\bsession with (?:the )?(?:standard )?tool"
    r"|\bpicked up by (?:a )?session\b"
    # (4) dispatch-provisioning phrase ("tool access") — only in a provisioning report
    r"|\btool access\b"
    # (5) no accessible checkout / working tree / working copy / repo access
    r"|\bno\b[^.]{0,30}?\b(?:accessible )?(?:checkout|working tree|working copy|repo(?:sitory)? access)\b"
    # (6) internal task-context tools (TaskGet/TaskList/TaskRead) returning nothing
    r"|\btask(?:get|list|read)\b[^.]{0,60}?\b(?:returned nothing|nothing|empty|unavailable|no rows)\b"
    # (7) inability to do tool-requiring work (the consequence phrasing of a lack)
    r"|\b(?:cannot|can't|can not|unable to|couldn't|could not)\b[^.]{0,60}?"
    r"\b(?:inspect|make code changes|run the required|run [^.]{0,20}?verify-gates|verify-gates"
    r"|clone|check ?out|apply the patch)\b"
    r")",
    re.IGNORECASE,
)


def is_tool_lack_selfreport(text: str) -> bool:
    """True if *text* is an agent's own "I lack the tools to proceed" dispatch fault.

    An agent that stops with ``needs_user_input`` because its session was
    dispatched WITHOUT the shell / ``gh`` / toolset / checkout its own work needs is
    reporting a DISPATCH fault — a phase mis-provisioned for its job — not asking the
    owner to decide anything. Surfacing that self-report to the owner's DM is the
    exact leak this classifier defends (it reached the owner as "*Pending question* …
    This session lacks any shell/write tool …", and later as the review-phase
    "launched without … tool access, so I cannot inspect the PR diff …" / "no shell,
    TaskGet/TaskList returned nothing" leaks). Such a reason is recorded ``INTERNAL``
    — logged / statusline-only, never DM'd. See ``_TOOL_LACK_SELFREPORT_RE``.
    """
    return bool(_TOOL_LACK_SELFREPORT_RE.search(_WHITESPACE_RE.sub(" ", text.strip())))


__all__ = ["is_tool_lack_selfreport", "question_fingerprint"]

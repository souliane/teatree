"""Recognise an UMBRELLA row — a tracking parent the factory must never claim (#4105).

An epic carries no single acceptance criterion and no bounded diff, so an agent handed
one holds a bounded ``issue_implementer_max_concurrent`` slot for an unbounded scope. It
is not that the run produces nothing — souliane/teatree#4048 produced a landable PR — it
is that "done" is undefined, so the slot is never released by the work finishing and a
real, implementable ticket is displaced for the whole run.

Three INDEPENDENT signals, any one sufficient:

* LABEL — a label in the operator-maintained ``umbrella_issue_labels`` set, resolved by
    :func:`~teatree.core.intake.factory_admission.resolve_umbrella_labels` and passed in.
    The shipped value lives in ``defaults.toml`` alone, so no caller holds a stale copy.
* DECLARATION — the row states of itself that it is never closed (souliane/teatree#2663).
* STRUCTURAL — the shape of a tracking parent: a checklist whose items are bare child
    issue references, and no acceptance criteria anywhere. Catches an unlabelled epic.

The ``Epic:`` title prefix is deliberately NOT a signal. It is a convention, and it stops
working the first time someone titles an epic differently — admitting it as a disjunct
would make the weakest signal sufficient on its own. An emphasised ``DO NOT CLOSE`` is
admitted on the opposite grounds: it is not a way of naming a category but an explicit
instruction that no state of the world ends the claim, which IS the unbounded-scope
property this module refuses. souliane/teatree#2663 — a standing ledger each dream pass
appends to — carries no label and lists prose checklist items, so it defeated both other
signals and a coding agent was dispatched at a row with nothing to implement.

A false positive STARVES a genuinely implementable issue (intake ignores it silently and
no surface says why), so all three branches are narrow: a prose ``Related: #N`` is not a
checklist item, a checklist of prose criteria is not a child list, any acceptance /
definition-of-done / expected-behaviour heading defeats the structural branch outright,
and a declaration counts only where :func:`_never_close_declaration` can tell it from a
sentence that merely contains the words.

The two EXPLICIT signals are tested before the inferred structural one, so a row carrying
several reports the reason its author actually wrote. An acceptance heading defeats only
the structural branch: it withdraws an inference, and there is no inference to withdraw
once the row has stated the answer itself.
"""

import re

from teatree.url_classify import find_forge_urls

#: One child link is a cross-reference; two is a list. Below this the structural branch
#: refuses, leaving the label branch to catch a one-child epic (souliane/teatree#4052).
_MIN_CHILD_REFS = 2

#: A checklist item carrying ONE bare token and nothing else — ``- [x] #3848`` or a bare
#: forge URL. Trailing prose disqualifies it: that is a work item, not a child. Whether the
#: token IS a child ref is decided by :func:`_is_child_ref`, never by a path shape spelled
#: here — the forge path grammar lives in :mod:`teatree.url_classify` and nowhere else.
_CHILD_REF_ITEM = re.compile(r"^[ \t]*[-*][ \t]*\[[ xX]\][ \t]*<?(?P<ref>\S+?)>?[ \t]*$", re.MULTILINE)

#: A bare same-repo issue reference, the shape teatree's own epics list children in.
_BARE_NUMBER_REF = re.compile(r"^#\d+$")

#: Any heading that promises a testable outcome. Its presence means the row states what
#: "done" looks like, which is exactly what an umbrella row does not.
#: Both spellings of "behaviour" are written out in full rather than shortened to one
#: optional-letter alternation: that shape leaves a word fragment the codespell hook
#: "corrects", silently rewriting the pattern so it stops matching one of them.
_ACCEPTANCE_HEADING = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]*\**[ \t]*"
    r"(?:acceptance|definition of done|success criteria|expected behaviour|expected behavior)\b",
    re.MULTILINE | re.IGNORECASE,
)


#: A heading, or a line opened with bold/strong marks, with leading decoration (an emoji, a
#: warning sign) dropped from the captured text. Emphasis is the discriminator: a standing row
#: announces itself, whereas "please don't close this yet" is a sentence and stays admissible.
_EMPHASISED_LINE = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]*|[*_]{2})[^0-9A-Za-z\n]*(?P<text>.*?)[ \t]*[*_]{0,2}[ \t]*$")

#: The directive, leading its line, optionally naming the row itself, and then ENDING at the
#: line's end, sentence-final punctuation, or a dash introducing an aside ("DO NOT CLOSE --
#: standing ledger"). A colon, comma, or an attached hyphen instead means the phrase runs on
#: to describe some other object ("do not close: the modal stays open", "close-fail the
#: socket") -- prose about a bug, not a declaration about the row.
_NEVER_CLOSE = re.compile(
    r"(?:do[ \t]*not|don't|never)[ \t-]*close"
    r"(?:[ \t]+th(?:is|e)[ \t]+(?:issue|ticket|row|ledger|page))?"
    r"[ \t]*(?:$|[.!?]|[\u2014\u2013])",
    re.IGNORECASE,
)

#: A commonmark fence marker (``` or ~~~), up to 3 leading spaces. A body that quotes the
#: declaration inside a fenced example -- documenting the detector, not invoking it -- must
#: not be read as a live declaration.
_FENCE = re.compile(r"^[ \t]{0,3}(?:```|~~~)")

#: How much of the declaration the reason quotes back — a log line is fixed-width, a heading is not.
_REASON_QUOTE_LIMIT = 60


def normalize_labels(labels: "frozenset[str] | set[str] | list[str] | tuple[str, ...]") -> frozenset[str]:
    """Casefold + strip every label — the ONE normalization both sides of the match use.

    Applied to the configured set and the payload's labels alike, so ``Epic`` on the issue
    matches ``epic`` in the setting without either side being stripped to force a match.
    """
    return frozenset(label.strip().casefold() for label in labels if label.strip())


def _is_child_ref(token: str) -> bool:
    """Whether *token* is ONE child reference and nothing else.

    A forge URL is recognised by :func:`~teatree.url_classify.find_forge_urls` — the single
    home for the PR/MR/issue path grammar — and must span the WHOLE token, so a link with a
    fragment or a query trailing the issue number is prose about a child, not the child.
    """
    return bool(_BARE_NUMBER_REF.match(token)) or find_forge_urls(token) == [token]


def _child_ref_count(body: str) -> int:
    """How many DISTINCT child issues the body lists as bare checklist items."""
    refs = {match["ref"] for match in _CHILD_REF_ITEM.finditer(body) if _is_child_ref(match["ref"])}
    return len(refs)


def _never_close_declaration(body: str) -> str:
    """The emphasised line on which *body* declares itself never closed, or ``""``.

    Three conditions are required of the same line, and no one of them is enough: the line
    must sit outside a fenced code block (a quoted example documents the detector, it does
    not invoke it), must be emphasised, and the directive must LEAD it and end there per
    :data:`_NEVER_CLOSE`. So a heading whose subject happens to be closing something is
    prose, and a paragraph asking to hold an issue open pending something is a temporary
    request against still-implementable work.
    """
    fenced = False
    for line in body.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        emphasised = _EMPHASISED_LINE.match(line)
        if emphasised is None:
            continue
        text = emphasised["text"].strip()
        if _NEVER_CLOSE.match(text):
            return text
    return ""


def umbrella_reason(*, body: str, labels: "frozenset[str]", umbrella_labels: "frozenset[str]") -> str:
    """Why *body*/*labels* read as an umbrella row, or ``""`` when they do not.

    The string is the log line's explanation, so a declined issue says which signal fired
    rather than vanishing from intake with no account of itself.
    """
    matched = sorted(normalize_labels(labels) & normalize_labels(umbrella_labels))
    if matched:
        return f"carries the {', '.join(repr(label) for label in matched)} label"
    declaration = _never_close_declaration(body)
    if declaration:
        return f"declares itself never closed: {declaration[:_REASON_QUOTE_LIMIT]}"
    children = _child_ref_count(body)
    if children >= _MIN_CHILD_REFS and not _ACCEPTANCE_HEADING.search(body):
        return f"lists {children} child issues as a checklist and states no acceptance criteria"
    return ""

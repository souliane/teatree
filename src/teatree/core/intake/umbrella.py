"""Recognise an UMBRELLA row — a tracking parent the factory must never claim (#4105).

An epic carries no single acceptance criterion and no bounded diff, so an agent handed
one holds a bounded ``issue_implementer_max_concurrent`` slot for an unbounded scope. It
is not that the run produces nothing — souliane/teatree#4048 produced a landable PR — it
is that "done" is undefined, so the slot is never released by the work finishing and a
real, implementable ticket is displaced for the whole run.

Two INDEPENDENT signals, either sufficient:

* LABEL — a label in the operator-maintained ``umbrella_issue_labels`` set, resolved by
    :func:`~teatree.core.intake.factory_admission.resolve_umbrella_labels` and passed in.
    The shipped value lives in ``defaults.toml`` alone, so no caller holds a stale copy.
* STRUCTURAL — the shape of a tracking parent: a checklist whose items are bare child
    issue references, and no acceptance criteria anywhere. Catches an unlabelled epic.

The ``Epic:`` title prefix is deliberately NOT a signal. It is a convention, and it stops
working the first time someone titles an epic differently — admitting it as a third
disjunct would make the weakest signal sufficient on its own.

A false positive STARVES a genuinely implementable issue (intake ignores it silently and
no surface says why), so both branches are narrow: a prose ``Related: #N`` is not a
checklist item, a checklist of prose criteria is not a child list, and any acceptance /
definition-of-done / expected-behaviour heading defeats the structural branch outright.
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


def umbrella_reason(*, body: str, labels: "frozenset[str]", umbrella_labels: "frozenset[str]") -> str:
    """Why *body*/*labels* read as an umbrella row, or ``""`` when they do not.

    The string is the log line's explanation, so a declined issue says which signal fired
    rather than vanishing from intake with no account of itself.
    """
    matched = sorted(normalize_labels(labels) & normalize_labels(umbrella_labels))
    if matched:
        return f"carries the {', '.join(repr(label) for label in matched)} label"
    children = _child_ref_count(body)
    if children >= _MIN_CHILD_REFS and not _ACCEPTANCE_HEADING.search(body):
        return f"lists {children} child issues as a checklist and states no acceptance criteria"
    return ""

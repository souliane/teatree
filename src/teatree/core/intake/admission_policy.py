"""The single per-overlay admission verdict every issue-intake path runs (#3573).

Two scanners turn an open issue into autonomous work: the assignment scanner
(``assigned_issues``) and the trusted-author scanner (``issue_implementer``).
Before either creates ``Ticket(role=author)`` + ``Task(phase=coding)`` it asks
:func:`admit_issue` — routing both through ONE decision fixes the "two intake
paths, divergent gates" problem, so the factory holds a single opinion of which
issue is auto-workable per overlay.

The overlay's :class:`~teatree.config.enums.AdmissionPolicy` (DB-overridable,
default ``ASSIGNED_AND_LABELED``) plus the issue's assignment and ``t3-auto``
label resolve into the verdict. The HARD INVARIANT is enforced as an explicit
floor BEFORE the policy branch: an issue BOTH unassigned to the owner AND lacking
the ``t3-auto`` label is NEVER auto-worked under any non-``all`` policy, so a
future policy tier can never regress it.
"""

from collections.abc import Iterable
from typing import cast

from teatree.config import get_effective_settings
from teatree.config.enums import AdmissionPolicy
from teatree.types import RawAPIDict

#: The label that admits an unassigned/colleague issue under a labeled policy.
AUTO_LABEL = "t3-auto"


def _normalise(handles: Iterable[str]) -> frozenset[str]:
    return frozenset(handle.strip().lower() for handle in handles if handle.strip())


def issue_assignees(issue: RawAPIDict) -> frozenset[str]:
    """The lower-cased handles an issue is assigned to, across both forges' shapes.

    GitLab uses ``username``, GitHub uses ``login``; both expose a singular
    ``assignee`` and a plural ``assignees`` list, so both are unioned.
    """
    handles: set[str] = set()
    plural = issue.get("assignees")
    if isinstance(plural, list):
        for item in plural:
            if isinstance(item, str):
                handles.add(item)
            elif isinstance(item, dict):
                handles.add(_handle_of(cast("RawAPIDict", item)))
    single = issue.get("assignee")
    if isinstance(single, str):
        handles.add(single)
    elif isinstance(single, dict):
        handles.add(_handle_of(cast("RawAPIDict", single)))
    return _normalise(handles)


def _handle_of(actor: RawAPIDict) -> str:
    for key in ("username", "login"):
        value = actor.get(key)
        if isinstance(value, str):
            return value
    return ""


def issue_labels(issue: RawAPIDict) -> frozenset[str]:
    """The label names on an issue, across the string and ``{"name": ...}`` shapes."""
    raw = issue.get("labels")
    if not isinstance(raw, list):
        return frozenset()
    names: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            name = cast("RawAPIDict", item).get("name")
            if isinstance(name, str):
                names.add(name)
    return frozenset(names)


def resolve_admission_policy(overlay: str) -> AdmissionPolicy:
    """The effective :class:`AdmissionPolicy` for *overlay* (env → DB → code → default)."""
    return get_effective_settings(overlay or None).admission_policy


def admits(policy: AdmissionPolicy, *, assigned: bool, labeled: bool) -> bool:
    """The pure verdict: does *policy* admit an issue with these two facts?

    The HARD INVARIANT floor is checked before the policy branch: an unassigned
    AND unlabeled issue is refused under every non-``all`` policy.
    """
    if policy is AdmissionPolicy.ALL:
        return True
    if not assigned and not labeled:
        return False
    if policy is AdmissionPolicy.ASSIGNED:
        return assigned
    return assigned and labeled


def admit_issue(issue: RawAPIDict, *, overlay: str, owner_handles: Iterable[str]) -> bool:
    """Whether *overlay*'s factory may AUTONOMOUSLY auto-work *issue*.

    The single source of truth both issue-intake scanners consume. Resolves the
    overlay policy, reads the issue's assignment against *owner_handles* and its
    ``t3-auto`` label, and applies :func:`admits` (which enforces the floor).
    """
    return admits(
        resolve_admission_policy(overlay),
        assigned=bool(_normalise(owner_handles) & issue_assignees(issue)),
        labeled=AUTO_LABEL in issue_labels(issue),
    )

"""GitHub rollup-entry dedupe: newest check-run per (typename, name) (module-health split of ci_rollup).

Split out of :mod:`ci_rollup` for module length — this group is a cohesive, self-contained
concern (the GitHub branch-protection "newest-per-check-name" dedupe rule) that
:func:`~teatree.core.merge.ci_rollup.classify_required_rollup` and
:func:`~teatree.core.merge.ci_rollup._github_actions_runs_verdict`'s sibling verdict
functions consume as a black box.
"""

from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from teatree.types import RawAPIDict


# One ``gh ... statusCheckRollup`` entry — a CheckRun or a legacy StatusContext.
# Declared with the FUNCTIONAL ``TypedDict`` syntax because ``__typename`` (the
# GraphQL discriminator the dedupe key reads via :func:`_check_identity`) is a
# dunder name that the class-based syntax cannot express — the class body mangles
# a leading-double-underscore attribute, so the key would silently not type-check.
_RollupEntry = TypedDict(
    "_RollupEntry",
    {
        "__typename": object,
        "conclusion": object,
        "status": object,
        "state": object,
        "name": object,
        "context": object,
        "startedAt": object,
        "completedAt": object,
        "createdAt": object,
    },
    total=False,
)


def _check_identity(entry: _RollupEntry) -> tuple[str, str]:
    """The dedupe key for one rollup entry: ``(typename, name)``.

    GitHub branch protection keys the newest check-run per check NAME within a
    namespace. A CheckRun's name is ``name``; a legacy StatusContext's name is
    ``context``. The ``__typename`` is part of the key so a CheckRun and a
    StatusContext that happen to share a name stay distinct identities (they are
    different check kinds the forge tracks separately).
    """
    typename = str(entry.get("__typename") or "")
    name = str(entry.get("name") or entry.get("context") or "")
    return (typename, name)


def _check_recency(entry: _RollupEntry) -> str:
    """The recency key for one rollup entry — newest wins on dedupe.

    CheckRun entries carry ISO-8601 ``completedAt`` / ``startedAt``; legacy
    StatusContext entries carry ``createdAt``. The lexicographic order of an
    ISO-8601 UTC timestamp is its chronological order, so plain string ``max``
    selects the newest entry. A missing timestamp sorts oldest (empty string),
    so a timestamped entry always supersedes an untimestamped one.
    """
    return str(entry.get("completedAt") or entry.get("startedAt") or entry.get("createdAt") or "")


def _dedupe_newest_per_name(rollup: "list[RawAPIDict]") -> "list[RawAPIDict]":
    """Reduce the rollup to the newest check-run per ``(typename, name)``.

    Matches GitHub branch-protection semantics: a cancelled/stale run that left
    a spurious FAILURE check-run on the head commit is superseded by a newer
    SUCCESS for the same name and must not block the merge. Entries with no
    identity (neither ``name`` nor ``context``) are kept as-is so a malformed
    rollup still classifies fail-closed via the existing per-entry path.
    """
    newest: dict[tuple[str, str], RawAPIDict] = {}
    unkeyed: list[RawAPIDict] = []
    for raw in rollup:
        if not isinstance(raw, dict):
            continue
        entry = cast("_RollupEntry", raw)
        identity = _check_identity(entry)
        if not identity[1]:
            unkeyed.append(dict(raw))
            continue
        incumbent = newest.get(identity)
        if incumbent is None or _check_recency(entry) >= _check_recency(cast("_RollupEntry", incumbent)):
            newest[identity] = dict(raw)
    return [*newest.values(), *unkeyed]

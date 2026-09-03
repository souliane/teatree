"""Whether a PR's branch is behind its base, computed from the two commits (#4526).

``mergeStateStatus`` cannot answer this. It is a SINGLE value with precedence,
not a set of flags: a PR that is behind AND has failing required checks reports
``BLOCKED``, behind AND conflicted reports ``DIRTY``, and — without the
"require branches to be up to date" protection — a behind PR reports ``CLEAN``.
It reports ``BEHIND`` only when nothing else is wrong, which is precisely never
for the stale-base remedy that consumes it.

Behind-ness is a property of two commits, so it is read as one:
``Ref.compare(headRef:).behindBy``. GraphQL aliases the whole open-PR set into a
single ~100-byte call per base ref, where the REST ``compare`` endpoint costs a
~75 KB response PER PR.

Pure — the ``gh`` call that carries these lives in
:mod:`teatree.loop.scanners.pr_sweep_adapters`.
"""

import json

__all__ = [
    "BEHIND_CHUNK_SIZE",
    "build_compare_query",
    "chunk_heads",
    "head_compare_ref",
    "parse_compare_response",
]

#: PRs per GraphQL query. ``gh pr list`` caps at 100, which one query would carry
#: fine today; splitting keeps a repo near that cap clear of the node-complexity
#: budget. Global safety constant, identical for every overlay.
BEHIND_CHUNK_SIZE = 50

_ALIAS_PREFIX = "p"


def head_compare_ref(*, head_ref: str, owner: str, cross_repo: bool) -> str:
    """The canonical, fully-qualified head for a compare.

    A fork's branch name is ambiguous against the base repo's own refs, so the
    ``owner:branch`` form is the canonical key and every reference is normalised
    UP to it here — the one boundary that names a head.
    """
    if cross_repo and owner:
        return f"{owner}:{head_ref}"
    return head_ref


def build_compare_query(*, owner: str, name: str, base_ref: str, heads: dict[int, str]) -> str:
    """One aliased ``behindBy`` read per PR, against one base ref."""
    aliases = "\n".join(
        f"      {_ALIAS_PREFIX}{number}: compare(headRef: {json.dumps(head)}) {{ behindBy }}"
        for number, head in sorted(heads.items())
    )
    return (
        "query {\n"
        f"  repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{\n"
        f"    ref(qualifiedName: {json.dumps(f'refs/heads/{base_ref}')}) {{\n"
        f"{aliases}\n"
        "    }\n"
        "  }\n"
        "}"
    )


def parse_compare_response(payload: str) -> dict[int, bool | None]:
    """Decode ``{pr number: is behind}``; ``None`` where the read did not answer.

    ``None`` is a third state on purpose — collapsing an unreadable alias to
    ``False`` is what made the old predicate report every behind PR as up to
    date. GitHub answers a partial failure with the resolved aliases AND a
    top-level ``errors`` block (and ``gh`` then exits non-zero), so the data that
    did come back is read here regardless of the exit code.
    """
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    data = decoded.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    ref = repository.get("ref") if isinstance(repository, dict) else None
    if not isinstance(ref, dict):
        return {}
    answers: dict[int, bool | None] = {}
    for alias, entry in ref.items():
        number = _alias_number(alias)
        if number is not None:
            answers[number] = _is_behind(entry)
    return answers


def chunk_heads(heads: dict[int, str], size: int = BEHIND_CHUNK_SIZE) -> list[dict[int, str]]:
    """Split *heads* into query-sized batches, preserving every entry."""
    ordered = sorted(heads.items())
    return [dict(ordered[start : start + size]) for start in range(0, len(ordered), size)]


def _alias_number(alias: str) -> int | None:
    suffix = alias[len(_ALIAS_PREFIX) :]
    if not alias.startswith(_ALIAS_PREFIX) or not suffix.isdigit():
        return None
    return int(suffix)


def _is_behind(entry: object) -> bool | None:
    if not isinstance(entry, dict):
        return None
    behind_by = entry.get("behindBy")
    if not isinstance(behind_by, int) or isinstance(behind_by, bool):
        return None
    return behind_by > 0

"""GitLab MR discussion-thread resolution + approvals-payload parsing.

The predicates :class:`~teatree.backends.gitlab.client.GitLabCodeHost` reads to
turn an MR's ``/discussions`` and ``/approvals`` payloads into merge-gating
counts: which threads are unresolved-resolvable, which are a review bot's own
stale threads (:func:`thread_opened_solely_by`), and how many approvals remain
(:func:`_read_int`). Split out of ``client.py`` so the host stays focused on the
cross-host Protocol surface — the same shape as the sibling ``pr_reads`` /
``issue_ops`` / ``uploads`` modules.
"""

from typing import cast

from teatree.types import RawAPIDict


def _read_int(data: RawAPIDict, key: str) -> int:
    """Return ``data[key]`` as an int, or ``-1`` when the key is absent / non-int.

    The sentinel ``-1`` lets callers distinguish "field missing in payload" from
    a legitimate zero. GitLab's approvals payload uses both ``int`` and ``str``
    encodings across versions, so we accept either.
    """
    value = data.get(key)
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return -1
    return -1


def _note_author(note: RawAPIDict) -> str:
    """Return a note's author username, or ``""`` when absent/null (#3340).

    GitLab system notes carry ``author: null`` (or omit the key); a naive
    ``note["author"]["username"]`` blows up mid-walk. An empty string means
    "no human author" — which never counts as "someone else" in
    :func:`thread_opened_solely_by`.
    """
    author = note.get("author")
    if not isinstance(author, dict):
        return ""
    username = cast("RawAPIDict", author).get("username")
    return username if isinstance(username, str) else ""


def thread_opened_solely_by(discussion: RawAPIDict, author: str) -> bool:
    """True when *author* opened *discussion* and no one else replied (#3340).

    The load-bearing "stale bot thread" predicate: a review bot posts inline
    threads, the author force-pushes, the threads now point at code that no
    longer exists — yet they still count as unresolved. This is *true* only when
    the thread's first note is *author*'s AND every other authored note is also
    *author*'s. A note whose author is null/absent (a system note) never counts
    as "someone else"; a single reply from a different username flips it to
    ``False`` — resolving such a thread would silently discard a real objection,
    the correctness failure worth encoding once here rather than per overlay.

    *author* is the bot username (overlay config, not core's business). A blank
    *author* returns ``False`` so a missing config never eats every thread.
    """
    if not author:
        return False
    notes = discussion.get("notes")
    if not isinstance(notes, list) or not notes:
        return False
    first = notes[0]
    if not isinstance(first, dict) or _note_author(cast("RawAPIDict", first)) != author:
        return False
    return all(_note_author(cast("RawAPIDict", note)) in {"", author} for note in notes if isinstance(note, dict))


def _count_unresolved_resolvable_threads(discussions: list[RawAPIDict], *, ignore_author: str = "") -> int:
    """Count open ``resolvable`` discussion threads — what blocks an MR merge.

    A thread is "unresolved-resolvable" when at least one of its notes is
    ``resolvable: true`` AND no note carries ``resolved: true`` (the missing key
    is treated as not-resolved — truthiness on an absent key would be a bug).
    System notes and non-resolvable comments are skipped: the GitLab "must
    resolve all threads" policy is keyed on the same ``resolvable`` flag.

    When *ignore_author* is set, threads opened solely by that author — a review
    bot's own stale threads with no reply from anyone else, per
    :func:`thread_opened_solely_by` — are EXCLUDED, so stale bot noise stops
    blocking auto-merge as hard as a human's unaddressed objection. The default
    (``""``) is byte-identical to the pre-#3340 count: no existing consumer
    shifts.
    """
    count = 0
    for disc in discussions:
        if not isinstance(disc, dict):
            continue
        notes_raw = disc.get("notes", [])
        if not isinstance(notes_raw, list):
            continue
        has_resolvable = False
        has_resolved = False
        for note in notes_raw:
            if not isinstance(note, dict):
                continue
            note_dict = cast("RawAPIDict", note)
            if note_dict.get("resolvable") is True:
                has_resolvable = True
            if note_dict.get("resolved") is True:
                has_resolved = True
        if has_resolvable and not has_resolved:
            if ignore_author and thread_opened_solely_by(disc, ignore_author):
                continue
            count += 1
    return count

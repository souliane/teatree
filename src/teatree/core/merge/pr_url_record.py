"""The one write that puts an opened PR on its ticket's record (souliane/teatree#4305).

TWO paths open a PR for a branch, and only one of them used to record the URL.
The ship executor does; the pre-push ``ensure-pr`` hook — which fires from inside
the ship's own ``push_branch`` — did not. So every refusal reachable AFTER that
push (the post-push fleet-claim fence, the no-URL / wrong-slug / 404 returns of
the PR-open half) left a PR live on the forge that the ticket had no record of.
The retry then collided with ``already exists`` while the live PR stayed
untracked — the #4151 loop, narrowed by #4304 but not closed.

Recording from both paths closes it: the retry reads the URL back and adopts the
existing PR instead of opening a second one. Re-recording is safe by construction
— ``merge_extra`` does not append an item the list already holds, and
``record_opened`` is ``get_or_create`` on the url — so the two paths racing over
one PR converge rather than duplicate.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from django.apps import apps

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import JSONObject


def record_pr_url(ticket: "Ticket", url: str, branch: str, *, pop_keys: Sequence[str] = ()) -> None:
    """Record *url* as *branch*'s PR on *ticket* — the JSON index and the arbiter row.

    #800 N3 + list-append: ``pr_urls`` / ``pr_url_by_branch`` are derived INSIDE
    the locked ``merge_extra`` from the re-read row, never from a run-start
    snapshot — replacing the whole list from a stale one dropped a concurrent
    ship's freshly-appended URL. #1263: the per-branch index is what lets a later
    workstream tell whether its OWN PR exists.

    #3840: that JSON index is the ticket's own cache; the ``PullRequest`` row is
    what the merge keystone, the board reconcile and the merge-evidence gate
    resolve a PR's owning ticket through. Both are written here, where the ticket
    is in hand and the URL has just been verify-by-re-read confirmed, so a PR that
    merges before the next tick is still attributable to its ticket.

    *pop_keys* clears caller-owned single-ship hints in the same locked write.
    """
    append_lists: dict[str, list[object]] = {"pr_urls": [url]} if url else {}
    merge_dicts: dict[str, JSONObject] = {"pr_url_by_branch": {branch: url}} if url and branch else {}
    ticket.merge_extra(
        append_to_lists=append_lists,
        merge_into_dicts=merge_dicts,
        pop_keys=list(pop_keys),
    )
    if url:
        apps.get_model("core", "PullRequest").objects.record_opened(ticket=ticket, url=url, overlay=ticket.overlay)

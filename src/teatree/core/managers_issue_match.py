"""Issue-URL alias matching for the ticket QuerySet.

Carved out of ``managers.py`` (mirroring managers_overlay.py / managers_task_claim.py)
to hold that flat-root queryset hub under the 500-LOC module-health cap. Takes a
queryset as its first argument rather than importing ``Ticket`` (mirrors
``managers_overlay.for_overlay``), so it stays a leaf with no ORM/app-registry
import edge for tach to flag.
"""

from typing import TYPE_CHECKING

from django.db import IntegrityError, models, transaction
from django.db.models import Q

from teatree.utils.url_slug import repo_namespaced_key

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def matching_issue_q(issue_url: str) -> Q:
    """Predicate for tickets that ARE the given issue — exact ``issue_url`` OR same ``repo_namespaced_key``.

    The collision-free ``repo_namespaced_key`` (#2293) collapses the URL
    aliases of one forge issue — GitLab's ``/-/issues/<n>`` vs the newer
    ``/-/work_items/<n>``, and a trailing slash — onto a single key the DB
    enforces UNIQUE (``unique_nonempty_repo_namespaced_key``). A sync upsert
    must therefore dedup on that key, not just the raw ``issue_url`` string:
    matching on ``issue_url`` alone misses a ticket already stored under a
    sibling alias, the upsert then INSERTs a second row, and ``save`` derives
    the *same* key and trips the constraint — aborting the whole followup
    sync. Falls back to the plain ``issue_url`` match when the key is blank
    (a PR/MR-keyed reviewer ticket, a bare-number or non-forge ``issue_url``),
    so those unaffected shapes stay byte-identical to a raw ``issue_url`` lookup.
    """
    ns_key = repo_namespaced_key(issue_url)
    predicate = Q(issue_url=issue_url)
    if ns_key:
        predicate |= Q(repo_namespaced_key=ns_key)
    return predicate


def get_or_create_ticket_by_issue(
    queryset: models.QuerySet, issue_url: str, **defaults: object
) -> tuple["Ticket", bool]:
    """Atomically get-or-create the one ticket for *issue_url* (dream-gap #1644241).

    Every ``Ticket.objects.create()`` anchored on a forge issue must be
    idempotent under concurrent syncs (an overlapping GitHub board sync and
    GitLab issue sync, or two overlapping intake ticks) — a bare
    ``matching_issue().first()`` check followed by a separate ``.create()``
    leaves a TOCTOU window where a racing writer inserts first and this call
    trips ``unique_nonempty_repo_namespaced_key`` instead of finding the row.
    Mirrors :meth:`~django.db.models.QuerySet.get_or_create`, but keyed on the
    alias-aware :func:`matching_issue_q` predicate rather than exact-kwarg
    equality, so a GitLab ``/-/work_items/<n>`` alias of an already-tracked
    ``/-/issues/<n>`` row is found rather than blindly re-inserted (#2293).
    """
    existing = queryset.filter(matching_issue_q(issue_url)).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            return queryset.create(issue_url=issue_url, **defaults), True
    except IntegrityError:
        existing = queryset.filter(matching_issue_q(issue_url)).first()
        if existing is None:
            raise
        return existing, False

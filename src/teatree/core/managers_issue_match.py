"""Issue-URL alias matching and create fields for the ticket QuerySet.

Carved out of ``managers.py`` (mirroring managers_overlay.py / managers_task_claim.py)
to hold that flat-root queryset hub under the 500-LOC module-health cap. It
needs no app-registry access.
"""

from typing import TYPE_CHECKING, TypedDict

from django.db.models import Q

from teatree.utils.url_slug import repo_namespaced_key

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class TicketCreateFields(TypedDict, total=False):
    overlay: str
    variant: str
    repos: list[str]
    kind: "Ticket.Kind"
    short_description: str


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


def reference_issue_q(ref: str) -> Q:
    bare, sep, fragment = ref.partition("#")
    if not sep or "=" in fragment:
        return matching_issue_q(ref)
    return matching_issue_q(ref) | matching_issue_q(bare)

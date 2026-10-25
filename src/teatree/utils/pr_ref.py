"""The canonical PR/MR reference value object: repo slug, PR/MR number, forge kind.

One frozen ``(slug, pr_id, host_kind)`` triple shared by every layer that names a
PR/MR — the URL parser (:func:`teatree.utils.url_slug.pr_ref_from_url`), the forge
classifier (:mod:`teatree.url_classify`), and the merge chokepoint's live-forge
queries (:class:`teatree.core.merge.ci_rollup.CodeHostQuery`). Bundling the three
into one object is what lets the merge layer drop the ``slug, pr_id, *, host_kind``
triple that used to thread through every ``fetch_*`` delegator.

``pr_id`` is the forge-agnostic name for the PR/MR number: a GitHub pull-request
number and a GitLab merge-request IID are the same field to every caller, and it
matches the ``pr_id`` keyword the :class:`~teatree.core.backend_protocols.CodeHostBackend`
merge RPCs already take. ``host_kind`` is ``"github"`` or ``"gitlab"`` — the same
transport switch the merge-execution backend resolution dispatches on.
"""

from dataclasses import dataclass

_HEAD_REF_BY_HOST = {"github": "refs/pull/{}/head", "gitlab": "refs/merge-requests/{}/head"}


@dataclass(frozen=True, slots=True)
class PrRef:
    """A parsed PR/MR reference: repo slug, PR/MR number, and forge transport kind."""

    slug: str
    pr_id: int
    host_kind: str = "github"

    @property
    def head_ref(self) -> str:
        """The read-only ref its forge publishes the head under, or ``""`` for an unknown forge."""
        return _HEAD_REF_BY_HOST.get(self.host_kind, "").format(self.pr_id)

    def same_pr(self, other: "PrRef") -> bool:
        """A blank host on either side cannot prove the two are different PRs."""
        hosts = {self.host_kind, other.host_kind}
        return (
            self.pr_id == other.pr_id
            and self.slug.strip().casefold() == other.slug.strip().casefold()
            and (len(hosts) == 1 or "" in hosts)
        )

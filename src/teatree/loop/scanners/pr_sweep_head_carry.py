"""The sweep's one call into the head carry-over (#5450).

Split out of :mod:`teatree.loop.scanners.pr_sweep` to keep that module under the module-health LOC cap.
"""

from teatree.core.merge.head_carry_over import LineageReader
from teatree.core.review.hold_carry_forward import carry_unchanged_holds
from teatree.loop.scanners.head_lineage import head_is_carried
from teatree.loop.scanners.pr_sweep_clear_lookup import ClearLookup, look_up_clear_for_head
from teatree.loop.scanners.pr_sweep_types import PrSummary


def look_up_clear(pr: PrSummary, *, reader: LineageReader | None) -> ClearLookup:
    """The clearance for *pr*'s live head, read again once its records followed an unchanged patch to it."""
    carry_unchanged_holds(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha)
    lookup = look_up_clear_for_head(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha)
    if (
        lookup.clear is None
        and reader is not None
        and head_is_carried(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha, host_kind=pr.host_kind, reader=reader)
    ):
        return look_up_clear_for_head(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha)
    return lookup

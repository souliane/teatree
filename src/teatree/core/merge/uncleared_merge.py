import logging

from django.db import transaction

from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.execution import execute_bound_merge, verified_merge_forge_account
from teatree.core.merge.post_hook import MergeAuditStamps, record_merge_and_advance
from teatree.core.models import MergeAudit, MergeClear
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.utils.pr_ref import PrRef

logger = logging.getLogger(__name__)


def merge_without_clear(*, ref: PrRef, expected_head_oid: str) -> str:
    query = CodeHostQuery.for_ref(ref)
    forge_account = verified_merge_forge_account(query)
    merged_sha = execute_bound_merge(ref=ref, expected_head_oid=expected_head_oid, query=query)
    try:
        if MergeAudit.objects.filter(merged_sha=merged_sha).exists():
            return merged_sha
        verdict = next(
            (
                row
                for row in ReviewVerdict.objects.verdicts_at(slug=ref.slug, pr_id=ref.pr_id, head_sha=expected_head_oid)
                if row.is_merge_safe()
            ),
            None,
        )
        if verdict is None:
            logger.error("merge landed for %s#%s without a matching merge-safe verdict", ref.slug, ref.pr_id)
            return merged_sha
        checks = query.required_checks_status()
        with transaction.atomic():
            if MergeAudit.objects.filter(merged_sha=merged_sha).exists():
                return merged_sha
            clear = MergeClear.objects.create(
                ticket=verdict.ticket,
                pr_id=ref.pr_id,
                slug=ref.slug,
                host_kind=ref.host_kind,
                reviewed_sha=expected_head_oid,
                reviewer_identity=verdict.reviewer_identity,
                gh_verify_result=verdict.gh_verify_result,
                blast_class=verdict.blast_class,
                actor_kind=MergeClear.ActorKind.FACTORY_LOOP,
                delegation="solo_overlay_sweep",
                merge_forge_account=forge_account,
            )
            record_merge_and_advance(
                clear=clear,
                merged_sha=merged_sha,
                required_checks_status=checks,
                repo_slug=ref.slug,
                authorizers=MergeAuditStamps(forge_account=forge_account),
            )
    except Exception:
        logger.exception("merge landed for %s#%s but its audit post hook failed", ref.slug, ref.pr_id)
    return merged_sha

"""The sweep's CI gate: forge dispatch + the verdict-based skip classifiers.

Split out of ``pr_sweep``/``pr_sweep_decision`` (module-health LOC/function-count
caps) — this is one cohesive concern: deciding the sweep's skip reason from the
CI state of the forge *pr* was read from.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from teatree.core.merge import CodeHostQuery
from teatree.core.modelkit.forge_readability import CHECKS_UNREADABLE
from teatree.loop.scanners.pr_sweep_decision import classify_sweep_ci
from teatree.loop.scanners.pr_sweep_types import GITLAB_PIPELINE_CHECK_NAME
from teatree.utils.pr_ref import PrRef

if TYPE_CHECKING:
    from teatree.loop.scanners.pr_sweep_types import PrSummary


def ci_gate_verdict(pr: "PrSummary", *, main_uv_audit_red: Callable[[], bool]) -> tuple[str | None, bool, set[str]]:
    """Delegate to the CI classifier of the forge *pr* was READ from (#12, #72, #4844).

    GitHub scopes to the branch-protection required set the keystone uses
    (:func:`~teatree.loop.scanners.pr_sweep_decision.classify_sweep_ci`); on a
    plan-restricted repo that set is indeterminate for a reason the sweep CAN
    resolve, so it takes the same Actions-API fallback the keystone falls back
    to (:func:`classify_plan_restricted_sweep_ci`) instead of skipping forever.
    GitLab answers from the head pipeline's status (:func:`classify_gitlab_sweep_ci`).
    """
    query = CodeHostQuery.for_ref(PrRef(slug=pr.slug, pr_id=pr.number, host_kind=pr.host_kind))
    if pr.host_kind == "gitlab":
        return classify_gitlab_sweep_ci(query.required_checks_status())
    required_names = query.required_context_names()
    if required_names is None and query.is_plan_restricted():
        return classify_plan_restricted_sweep_ci(query.plan_restricted_actions_verdict())
    return classify_sweep_ci(list(pr.rollup), required_names, main_uv_audit_red=main_uv_audit_red)


def classify_gitlab_sweep_ci(verdict: str) -> tuple[str | None, bool, set[str]]:
    """The sweep's CI decision on GitLab, from the head pipeline's own verdict.

    GitLab exposes no branch-protection required-context set — its backend answers
    ``[]``, which :func:`classify_sweep_ci` (via ``classify_required_rollup``) would
    read as "no gate configured" and classify GREEN whatever the pipeline did, so
    the GitLab arm reads :meth:`CodeHostQuery.required_checks_status` instead: the
    head pipeline's status, which already aggregates the required jobs
    server-side and fails closed to ``pending`` when no pipeline ran.

    The uv-audit fallback never applies (there is no per-check verdict to compare
    against ``main``), so the middle element is always ``False``.
    """
    return _classify_aggregated_verdict(verdict, red_checks={GITLAB_PIPELINE_CHECK_NAME})


def classify_plan_restricted_sweep_ci(verdict: str) -> tuple[str | None, bool, set[str]]:
    """The sweep's CI decision on a plan-restricted GitHub repo (#4844).

    Branch protection is unreadable on GitHub Free (every required-status-check
    endpoint 403s with the plan-restriction body), so there is no required-context
    set to scope a rollup to via ``classify_required_rollup``. Classifies from
    the already-aggregated :func:`~teatree.core.merge.ci_rollup._github_actions_runs_verdict`
    the same way :func:`classify_gitlab_sweep_ci` classifies GitLab's head-pipeline
    verdict — the uv-audit fallback never applies (no per-check verdict to compare
    against ``main``).
    """
    return _classify_aggregated_verdict(verdict, red_checks=set())


def _classify_aggregated_verdict(verdict: str, *, red_checks: set[str]) -> tuple[str | None, bool, set[str]]:
    if verdict == "green":
        return None, False, set()
    if verdict == "pending":
        return "ci_pending", False, set()
    # A read that failed saw no red: calling it ``ci_red`` let the stale-base remedy merge-update the branch.
    if verdict == CHECKS_UNREADABLE:
        return "required_checks_indeterminate", False, set()
    return "ci_red", False, red_checks

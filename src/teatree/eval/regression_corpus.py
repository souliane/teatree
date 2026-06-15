"""Deterministic regression tests — real code-path assertions per failure class.

A behavioral scenario (``scenarios/*.yaml``) grades what an agent *says* it
would do. This corpus grades what the gate/checker code *does*: each check
calls the REAL function (the merge-precondition assertion, the branch-currency
conflict predictor, the loop-lease pid-anchored
claim, the migration-graph leaf checker) on a constructed must-block input and
on a must-allow input, and reports a violation when either direction is wrong.

This is a Layer-1 test per ``README.md`` — deterministic, free, no ``claude``
run — sibling of :mod:`teatree.eval.trigger_qa`. It exists so the recurring
safety-gate failure classes of the last development cycle each have one check
that would go RED on the pre-fix behavior and stays GREEN on the fix, surfaced
through ``t3 eval pinned-regressions`` and the ``eval-pinned-regressions`` prek
pre-push hook.

Each :class:`RegressionCheck` names its failure class, the originating fix, and
a callable that returns ``True`` when the real code path still honors the
invariant. A check that needs a git repo builds a throwaway one under a
``tempfile.TemporaryDirectory`` and tears it down; a check that needs the ORM
is skipped unless Django is configured (the CLI bootstraps it). No network, no
secrets, no shared state.

The bulk of the ``_check_*`` predicates live in
:mod:`teatree.eval.regression_corpus_predicates`; the migration-fork predicate
stays here so its anti-vacuous test can patch ``_count_core_leaves`` on this
module's namespace, and the runtime self-DB schema pre-flight (#2190) lives in
:mod:`teatree.eval.regression_corpus_schema`.
"""

from teatree.eval.regression_corpus_e2e import (
    check_e2e_test_plan_embeds_claimable_relative_ref,
    check_e2e_test_plan_uploads_to_note_project,
)
from teatree.eval.regression_corpus_models import CheckResult, RegressionCheck, RegressionReport
from teatree.eval.regression_corpus_predicates import (
    _check_account_switch_detect_and_recover,
    _check_banned_terms_scanner_fails_closed_on_crash,
    _check_branch_currency_conflict_only,
    _check_forge_resolves_by_host_not_token,
    _check_loop_owner_lease_pid_anchored,
    _check_merge_precondition_maker_is_not_checker,
    _check_merge_precondition_substrate_full_autonomy,
    _check_merge_precondition_substrate_human_authorize,
    _check_mr_description_first_line_validated,
    _check_private_repo_allowlist_path_segment_match,
    _check_ship_branch_reconcile_renamed,
)
from teatree.eval.regression_corpus_report import render_json, render_text
from teatree.eval.regression_corpus_schema import schema_preflight_result

__all__ = [
    "CheckResult",
    "RegressionCheck",
    "RegressionReport",
    "render_json",
    "render_text",
    "run_regression_corpus",
]


def _count_core_leaves(graph: object) -> int:
    """Number of leaf nodes the ``core`` app owns in a migration graph.

    A linear graph has exactly one; a fork (two migrations off one parent)
    leaves two. The predicate the regression check turns on, factored out so a
    test can feed it a synthetic forked graph and assert it returns ``> 1``.
    """
    return sum(1 for leaf in graph.leaf_nodes() if leaf[0] == "core")  # type: ignore[attr-defined]


def _check_migration_graph_single_leaf() -> bool:
    """#1721: the migration graph stays linear — a forked graph (>1 leaf) is caught.

    The real failure: two PRs each branch a migration off the same parent, the
    merged graph has multiple leaf nodes, and ``migrate`` refuses. This asserts
    the live ``teatree.core`` graph has exactly one leaf node via
    :func:`_count_core_leaves` — the same predicate a synthetic forked graph
    drives ``> 1`` in the corpus's anti-vacuous test.
    """
    from django.db.migrations.loader import MigrationLoader  # noqa: PLC0415

    loader = MigrationLoader(None, ignore_no_migrations=True)
    return _count_core_leaves(loader.graph) == 1


_CHECKS: tuple[RegressionCheck, ...] = (
    RegressionCheck(
        failure_class="branch-currency §940 (conflict-only, never behind-only)",
        origin="https://github.com/souliane/teatree/pull/1719",
        invariant="sha_conflicts_with_target blocks a real conflict, allows a behind-but-clean SHA",
        predicate=_check_branch_currency_conflict_only,
    ),
    RegressionCheck(
        failure_class="substrate-merge human-authorize floor",
        origin="https://github.com/souliane/teatree/pull/1498",
        invariant="a below-full substrate MergeClear never merges without the recorded human authorizer",
        predicate=_check_merge_precondition_substrate_human_authorize,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="substrate-merge full-autonomy carve-out",
        origin="https://github.com/souliane/teatree/issues/1748",
        invariant="an autonomy=full overlay clears a ticket-less substrate CLEAR without a per-PR human sign-off",
        predicate=_check_merge_precondition_substrate_full_autonomy,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="maker≠checker at merge time",
        origin="https://github.com/souliane/teatree/pull/1601",
        invariant="a self-issued CLEAR (reviewer == executing loop) is refused at merge time",
        predicate=_check_merge_precondition_maker_is_not_checker,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="loop-owner hijack / pid-anchored lease",
        origin="https://github.com/souliane/teatree/pull/1724",
        invariant="an alive foreign owner past TTL is never hijacked; a dead owner is reclaimable",
        predicate=_check_loop_owner_lease_pid_anchored,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="migration-fork / multiple-leaf-nodes",
        origin="https://github.com/souliane/teatree/pull/1721",
        invariant="the live core migration graph has exactly one leaf; a forked graph is detectable",
        predicate=_check_migration_graph_single_leaf,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="account-switch detect-invalidate-reprobe (#1916)",
        origin="https://github.com/souliane/teatree/issues/1916",
        invariant="a /login switch invalidates the backend cache and re-probes; same account is a no-op",
        predicate=_check_account_switch_detect_and_recover,
    ),
    RegressionCheck(
        failure_class="private-repo allowlist path-segment match (security, #1953)",
        origin="https://github.com/souliane/teatree/pull/2084",
        invariant="private_repos matches path segments, not substring; an alias-glued public slug never downgrades",
        predicate=_check_private_repo_allowlist_path_segment_match,
    ),
    RegressionCheck(
        failure_class="banned-terms scanner fail-closed on crash (security, #1954)",
        origin="https://github.com/souliane/teatree/pull/2079",
        invariant="a crashing scanner returns SCANNER_UNAVAILABLE_MARKER (gate blocks), never None; a no-op is None",
        predicate=_check_banned_terms_scanner_fails_closed_on_crash,
    ),
    RegressionCheck(
        failure_class="forge backend by origin host, not token precedence (#2085)",
        origin="https://github.com/souliane/teatree/pull/2085",
        invariant="forge_from_remote keys on the repo host (github/gitlab/empty), regardless of configured PATs",
        predicate=_check_forge_resolves_by_host_not_token,
    ),
    RegressionCheck(
        failure_class="pre-push gates reconcile a renamed/stale branch (#1587)",
        origin="https://github.com/souliane/teatree/pull/2102",
        invariant=(
            "resolve_and_reconcile_branch adopts the prefixed current branch; "
            "falls back to the recorded one on an unrelated ref"
        ),
        predicate=_check_ship_branch_reconcile_renamed,
        needs_db=True,
    ),
    RegressionCheck(
        failure_class="MR description first-line validated client-side (#1367)",
        origin="https://github.com/souliane/teatree/pull/2098",
        invariant="validate_mr_metadata rejects a non-conventional first line and accepts a conventional one",
        predicate=_check_mr_description_first_line_validated,
    ),
    RegressionCheck(
        failure_class="e2e-test-plan embeds claimable relative /uploads ref (#2165 regression)",
        origin="https://github.com/souliane/teatree/issues/2165",
        invariant=(
            "_verified_embed embeds the relative /uploads/<secret>/<file> reference GitLab claims on "
            "save; never the absolute /-/project/ or any https:// upload URL"
        ),
        predicate=check_e2e_test_plan_embeds_claimable_relative_ref,
    ),
    RegressionCheck(
        failure_class="e2e-test-plan uploads to the note's own project, not a 2nd repo",
        origin="https://github.com/souliane/teatree/pull/2181",
        invariant=(
            "post_test_plan_comment uploads every artifact to repo_for_issue_url(issue_url) — the note's "
            "own project — never the manifest's second/CI repo, so the note's /uploads refs resolve"
        ),
        predicate=check_e2e_test_plan_uploads_to_note_project,
    ),
)


def _django_ready() -> bool:
    try:
        from django.apps import apps  # noqa: PLC0415
    except ImportError:
        return False
    return apps.ready


def run_regression_corpus(checks: tuple[RegressionCheck, ...] = _CHECKS) -> RegressionReport:
    db_ready = _django_ready()
    results: list[CheckResult] = []
    # Schema pre-flight (#2190): the ORM-backed checks query the runtime-resolved
    # self-DB, which (for a worktree's auto-isolated copy) is a stale-schema
    # snapshot never migrated. Bring it current in-process before any ORM check
    # so a migration-adding PR can't red the pre-push lane with OperationalError.
    if db_ready and any(check.needs_db for check in checks):
        results.append(schema_preflight_result())
    for check in checks:
        if check.needs_db and not db_ready:
            results.append(CheckResult(check=check, ok=True, skipped=True, detail="Django not configured"))
            continue
        try:
            ok = check.predicate()
            results.append(CheckResult(check=check, ok=ok, skipped=False, detail="" if ok else "invariant violated"))
        except Exception as exc:  # noqa: BLE001 — a raising predicate IS a regression failure, not a crash.
            results.append(CheckResult(check=check, ok=False, skipped=False, detail=f"{type(exc).__name__}: {exc}"))
    return RegressionReport(results=tuple(results))

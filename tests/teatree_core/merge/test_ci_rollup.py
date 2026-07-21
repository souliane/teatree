"""GitHub required-checks rollup classification (BLUEPRINT §17.4.3 step 3).

``CodeHostQuery.required_checks_status`` re-verifies the live required-checks at merge
time. The authoritative required set is the repo's branch-protection
``required_status_checks`` contexts — NOT the whole ``statusCheckRollup`` (which
reports every check on the head, required or not). A check NOT in the required
set (``eval``, advisory lanes) never blocks the merge regardless of its
conclusion; a branch-protection-required check that is failing/pending/missing
still refuses (fail closed). GitHub branch protection keys the *newest* check-run
per context name, so a cancelled/stale FAILURE superseded by a newer SUCCESS for
the same name must NOT block.

``TestRequiredContextsGate`` pins the required-set filtering (incl. the three
anti-vacuous merge-allow/refuse cases and the fail-closed branch-protection
lookup); ``TestDedupeNewestPerName`` pins the newest-per-name dedupe over a set
where every rolled-up name is required. Only the unstoppable external — the
``gh`` subprocess — is stubbed; the classification under test is real.
"""

import json
from collections.abc import Callable
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.backends.forge_merge_rpc import GhMergeRpc
from teatree.core.backend_protocols import CHANGED_PATHS_UNAVAILABLE, changed_paths_unavailable, rollup_query_failed
from teatree.core.merge import CodeHostQuery, classify_required_rollup, failing_required_names
from teatree.core.merge.ci_rollup import (
    _check_identity,
    _check_name,
    _classify_gitlab_pipeline,
    _dedupe_newest_per_name,
    _expected_required_contexts_floor,
    _required_contexts_verdict,
    attach_touched_paths,
)
from teatree.core.models import MergeClear
from teatree.utils.pr_ref import PrRef

_SLUG = "souliane/teatree"
_PR_ID = 2580


class _SeqRunner:
    """A ``Runner`` that returns queued ``(rc, out, err)`` triples and records calls."""

    def __init__(self, results: list[tuple[int, str, str]]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        return self._results.pop(0)


def _seq_runner(results: list[tuple[int, str, str]]) -> _SeqRunner:
    return _SeqRunner(results)


class _StubQuery:
    """A minimal stand-in for ``CodeHostQuery`` — only ``ref`` + ``pr_changed_paths``."""

    def __init__(self, paths: list[str], *, raises: bool = False) -> None:
        self.ref = PrRef(slug=_SLUG, pr_id=_PR_ID)
        self._paths = paths
        self._raises = raises

    def pr_changed_paths(self) -> list[str]:
        if self._raises:
            msg = "diff fetch boom"
            raise RuntimeError(msg)
        return self._paths


def _rollup_names(rollup: list[dict[str, object]]) -> list[str]:
    """The distinct check names in *rollup* (CheckRun ``name`` / StatusContext ``context``)."""
    names: list[str] = []
    for entry in rollup:
        if isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("context") or "")
            if name and name not in names:
                names.append(name)
    return names


def _rules_payload(*contexts: str) -> str:
    """A ``repos/<slug>/rules/branches/<base>`` body with one ``required_status_checks`` rule.

    Mirrors the effective-rules endpoint the fine-grained PAT CAN read: a JSON list
    of rules, each with a ``type``; the ``required_status_checks`` rule carries the
    required contexts under ``parameters.required_status_checks[].context``.
    """
    return json.dumps(
        [
            {"type": "pull_request", "parameters": {}},
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": ctx} for ctx in contexts],
                    "strict_required_status_checks_policy": True,
                },
            },
        ],
    )


# The default rules-endpoint response for the legacy stubs: an INDETERMINATE read
# (5xx) so a failing protection endpoint reproduces the pre-fix "both sources
# unreadable → fail closed" behaviour instead of being rescued by a readable
# rules endpoint. Tests that exercise the rules-endpoint rescue script it explicitly.
_RULES_UNREADABLE: tuple[int, str, str] = (1, "", "HTTP 500: server error")


def _gh_stub(
    rollup: list[dict[str, object]],
    *,
    required: list[str] | None = None,
    protection_rc: int = 0,
    protection_body: str | None = None,
    rules: tuple[int, str, str] = _RULES_UNREADABLE,
) -> Callable[[list[str]], tuple[int, str, str]]:
    """A ``gh`` runner answering the statusCheckRollup, rules, AND branch-protection queries.

    *required* is the branch-protection ``required_status_checks`` context set the
    repo reports. When ``None`` it defaults to every name present in *rollup* — so
    the dedupe tests, which treat all checks as required, keep their semantics.
    *protection_rc* / *protection_body* simulate a failed (or "no protection")
    branch-protection fetch — the fail-closed / no-gate paths. *rules* scripts the
    PAT-readable effective-rules endpoint; it defaults to an indeterminate read so a
    failing protection endpoint alone still fails closed.
    """
    contexts = _rollup_names(rollup) if required is None else required

    def run(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "statusCheckRollup" in joined:
            return (0, json.dumps(rollup), "")
        if "baseRefName" in joined:
            return (0, "main", "")
        if "rules/branches" in joined:
            return rules
        if "required_status_checks" in joined:
            if protection_rc != 0:
                return (protection_rc, "", protection_body or "api error")
            return (0, json.dumps({"contexts": contexts}), "")
        return (0, "", "")

    return run


def _verdict(
    rollup: list[dict[str, object]],
    *,
    required: list[str] | None = None,
    protection_rc: int = 0,
    protection_body: str | None = None,
    rules: tuple[int, str, str] = _RULES_UNREADABLE,
) -> str:
    stub = _gh_stub(
        rollup,
        required=required,
        protection_rc=protection_rc,
        protection_body=protection_body,
        rules=rules,
    )
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub):
        return CodeHostQuery.for_ref(PrRef(slug=_SLUG, pr_id=_PR_ID)).required_checks_status()


def _check_run(
    name: str,
    *,
    status: str = "COMPLETED",
    conclusion: str = "SUCCESS",
    started_at: str,
    completed_at: str,
) -> dict[str, object]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": started_at,
        "completedAt": completed_at,
    }


def _status_context(name: str, *, state: str, created_at: str) -> dict[str, object]:
    return {
        "__typename": "StatusContext",
        "context": name,
        "state": state,
        "createdAt": created_at,
    }


_T0 = "2026-06-19T10:00:00Z"
_T1 = "2026-06-19T10:05:00Z"

# The real souliane/teatree branch-protection required_status_checks contexts.
_REQUIRED = ["lint", "test (3.13)", "docs-drift", "uv-audit", "sbom", "blueprint-cross-pr"]


def _green(name: str) -> dict[str, object]:
    return _check_run(name, conclusion="SUCCESS", started_at=_T0, completed_at=_T1)


def _failed(name: str) -> dict[str, object]:
    return _check_run(name, conclusion="FAILURE", started_at=_T0, completed_at=_T1)


def _all_required_green() -> list[dict[str, object]]:
    return [_green(name) for name in _REQUIRED]


class TestRequiredContextsGate:
    """The branch-protection required-set filter — §17.4.3 step 3 (the #2769/#2770 bug).

    The keystone wrongly refused to merge PRs whose only non-success check was
    ``eval`` (a metered behavioral lane that is NOT a branch-protection-required
    context) — it treated EVERY rolled-up check as required. The fix scopes the
    verdict to the repo's branch-protection ``required_status_checks`` contexts.
    """

    def test_all_required_green_plus_non_required_failed_is_green(self) -> None:
        # Anti-vacuous #1 (RED on the pre-fix code): every branch-protection-
        # required context is SUCCESS; the ONLY failing check is ``eval`` — a
        # non-required metered lane. The merge must be ALLOWED (green). On the
        # buggy code the failed ``eval`` made the whole rollup "failed".
        rollup = [*_all_required_green(), _failed("eval")]
        assert _verdict(rollup, required=_REQUIRED) == "green"

    def test_non_required_pending_or_skipped_never_blocks(self) -> None:
        # A non-required check that is pending or skipped is equally irrelevant —
        # only the required set decides the verdict.
        rollup = [
            *_all_required_green(),
            _check_run("eval", status="IN_PROGRESS", conclusion="", started_at=_T0, completed_at=""),
            _check_run("advisory-lane", conclusion="SKIPPED", started_at=_T0, completed_at=_T1),
        ]
        assert _verdict(rollup, required=_REQUIRED) == "green"

    def test_required_check_failed_is_refused(self) -> None:
        # Anti-vacuous #2 (RED if the fix over-relaxes): a branch-protection-
        # REQUIRED context (``test (3.13)``) is FAILURE — the merge must be
        # REFUSED even though every non-required check is green.
        rollup = [_green(n) for n in _REQUIRED if n != "test (3.13)"]
        rollup += [_failed("test (3.13)"), _green("eval")]
        assert _verdict(rollup, required=_REQUIRED) == "failed"

    def test_required_check_pending_is_refused(self) -> None:
        # Anti-vacuous #3a: a required context still running → pending → refused.
        rollup = [_green(n) for n in _REQUIRED if n != "sbom"]
        rollup += [_check_run("sbom", status="IN_PROGRESS", conclusion="", started_at=_T0, completed_at="")]
        assert _verdict(rollup, required=_REQUIRED) == "pending"

    def test_required_check_missing_from_rollup_is_refused(self) -> None:
        # Anti-vacuous #3b: a required context with NO reporting check at all
        # (never started / not yet reported) → pending → refused. The rollup is
        # all-green but is missing the required ``blueprint-cross-pr`` context.
        rollup = [_green(n) for n in _REQUIRED if n != "blueprint-cross-pr"]
        assert _verdict(rollup, required=_REQUIRED) == "pending"

    def test_no_required_gate_configured_is_green(self) -> None:
        # The base branch has no required-status-check protection (empty contexts):
        # there is no gate to satisfy, so a failing non-required check cannot block.
        rollup = [_failed("eval"), _green("lint")]
        assert _verdict(rollup, required=[]) == "green"

    def test_branch_protection_fetch_failure_fails_closed(self) -> None:
        # Fail CLOSED: when the branch-protection required_status_checks endpoint
        # cannot be read (403 no-admin, 5xx, network), the required set is
        # indeterminate and the merge is REFUSED — never falls open to green.
        rollup = _all_required_green()
        assert _verdict(rollup, protection_rc=1, protection_body="HTTP 403: Forbidden") == "failed"

    def test_branch_not_protected_404_is_no_gate_green(self) -> None:
        # A determinate "Branch not protected" 404 is NOT a fetch failure — it
        # means no required-status-check gate exists, so the merge is allowed.
        rollup = [_failed("eval")]
        assert _verdict(rollup, protection_rc=1, protection_body="HTTP 404: Branch not protected") == "green"


class TestRulesEndpointRequiredSetResolution:
    """The §17.4.3-step-3 required set resolves from the PAT-readable rules endpoint.

    A fine-grained PAT WITHOUT "Administration" gets HTTP 403 on the legacy
    ``branches/<base>/protection/required_status_checks`` endpoint, but CAN read
    ``repos/<slug>/rules/branches/<base>``. The verdict must resolve the required
    set from the readable rules endpoint so an all-green PR merges — while a
    genuinely indeterminate required set (NEITHER endpoint readable) still fails
    closed (souliane/teatree merge-gate 403 bug).
    """

    _PAT_403 = "HTTP 403: Resource not accessible by personal access token"

    def test_403_protection_rescued_by_rules_all_green(self) -> None:
        # Protection 403s (no Admin); the rules endpoint IS readable and lists the
        # required contexts. Every required context is green → the merge is ALLOWED.
        rollup = [_green("lint"), _green("test (3.13)"), _failed("eval")]
        verdict = _verdict(
            rollup,
            protection_rc=1,
            protection_body=self._PAT_403,
            rules=(0, _rules_payload("lint", "test (3.13)"), ""),
        )
        assert verdict == "green"

    def test_403_protection_rescued_by_rules_failed_required_still_blocks(self) -> None:
        # The real-failure path must not regress: protection 403, rules readable and
        # requiring ``test (3.13)``, whose live check is FAILURE → still REFUSED.
        rollup = [_green("lint"), _failed("test (3.13)")]
        verdict = _verdict(
            rollup,
            protection_rc=1,
            protection_body=self._PAT_403,
            rules=(0, _rules_payload("lint", "test (3.13)"), ""),
        )
        assert verdict == "failed"

    def test_403_protection_and_rules_have_no_required_rule_is_green(self) -> None:
        # Protection 403, and the readable rules endpoint has NO required_status_checks
        # rule → determinate "no gate" → green (mergeable), NOT fail-closed.
        rollup = [_failed("eval")]
        verdict = _verdict(
            rollup,
            protection_rc=1,
            protection_body=self._PAT_403,
            rules=(0, json.dumps([{"type": "pull_request", "parameters": {}}]), ""),
        )
        assert verdict == "green"

    def test_both_endpoints_unreadable_fails_closed(self) -> None:
        # The fail-closed invariant: NEITHER the rules endpoint NOR the protection
        # endpoint could be read (both 5xx / non-deterministic) → the required set
        # is genuinely indeterminate → the merge is REFUSED.
        rollup = _all_required_green()
        verdict = _verdict(
            rollup,
            protection_rc=1,
            protection_body=self._PAT_403,
            rules=(1, "", "HTTP 500: server error"),
        )
        assert verdict == "failed"


class TestDedupeNewestPerName:
    def test_stale_failure_superseded_by_newer_success_is_green(self) -> None:
        # The PR #2580 incident: a cancelled stale-merge-ref run left
        # sbom=FAILURE on the same head, while the authoritative newer run
        # has sbom=SUCCESS. GitHub branch protection keys the newest per
        # name, so mergeStateStatus is CLEAN — the verdict must be green.
        rollup = [
            _check_run(
                "sbom",
                conclusion="FAILURE",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
        ]
        assert _verdict(rollup) == "green"

    def test_newest_entry_genuinely_failing_is_failed(self) -> None:
        # An older SUCCESS superseded by a newer genuine FAILURE for the same
        # name must still fail — newest wins in both directions.
        rollup = [
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
            _check_run(
                "sbom",
                conclusion="FAILURE",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
        ]
        assert _verdict(rollup) == "failed"

    def test_newest_entry_pending_is_pending(self) -> None:
        # An older SUCCESS superseded by a newer still-running run for the same
        # name is pending — the head is not yet conclusively green.
        rollup = [
            _check_run(
                "build",
                conclusion="SUCCESS",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
            _check_run(
                "build",
                status="IN_PROGRESS",
                conclusion="",
                started_at="2026-06-19T11:00:00Z",
                completed_at="",
            ),
        ]
        assert _verdict(rollup) == "pending"

    def test_mixed_checkrun_and_status_context_dedupe_each_namespace(self) -> None:
        # A CheckRun named "lint" (stale FAILURE → newer SUCCESS) and a
        # StatusContext named "coverage" (stale failure → newer success) both
        # dedupe within their own namespace; the verdict is green.
        rollup = [
            _check_run(
                "lint",
                conclusion="FAILURE",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
            _check_run(
                "lint",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
            _status_context("coverage", state="FAILURE", created_at="2026-06-19T10:00:00Z"),
            _status_context("coverage", state="SUCCESS", created_at="2026-06-19T11:00:00Z"),
        ]
        assert _verdict(rollup) == "green"

    def test_status_context_stale_failure_superseded_by_newer_success_is_green(self) -> None:
        rollup = [
            _status_context("legacy-ci", state="FAILURE", created_at="2026-06-19T10:00:00Z"),
            _status_context("legacy-ci", state="SUCCESS", created_at="2026-06-19T11:00:00Z"),
        ]
        assert _verdict(rollup) == "green"

    def test_distinct_names_each_failing_still_failed(self) -> None:
        # Two genuinely distinct failing checks (different names) must both
        # count — dedupe must not collapse distinct names into one.
        rollup = [
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
            _check_run(
                "tests",
                conclusion="FAILURE",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
        ]
        assert _verdict(rollup) == "failed"

    def test_checkrun_and_status_context_same_name_are_distinct_identities(self) -> None:
        # A CheckRun named "x" (SUCCESS) and a StatusContext named "x"
        # (FAILURE) are different identities (different __typename), so the
        # FAILURE is NOT superseded by the CheckRun — verdict is failed.
        rollup = [
            _check_run(
                "x",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
            _status_context("x", state="FAILURE", created_at="2026-06-19T10:00:00Z"),
        ]
        assert _verdict(rollup) == "failed"

    def test_dedupe_is_order_independent_newest_success_first(self) -> None:
        # Same incident as the first test but the newer SUCCESS is listed
        # BEFORE the stale FAILURE — the incumbent (newer) must not be
        # clobbered by an older entry encountered later. Verdict stays green.
        rollup = [
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
            _check_run(
                "sbom",
                conclusion="FAILURE",
                started_at="2026-06-19T10:00:00Z",
                completed_at="2026-06-19T10:05:00Z",
            ),
        ]
        assert _verdict(rollup) == "green"


class TestDedupeHelperEdgeCases:
    """Defensive branches of ``_dedupe_newest_per_name`` reached directly.

    The backend pre-filters non-dicts out of the rollup, so these shapes do
    not reach the helper through the public ``CodeHostQuery.required_checks_status``
    path — but the helper is defensive in its own right, so its guards are
    exercised here.
    """

    def test_non_dict_entry_is_skipped(self) -> None:
        rollup = [
            "not-a-dict",
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
        ]
        deduped = _dedupe_newest_per_name(rollup)
        assert deduped == [
            {
                "__typename": "CheckRun",
                "name": "sbom",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-06-19T11:00:00Z",
                "completedAt": "2026-06-19T11:05:00Z",
            },
        ]

    def test_entry_with_no_identity_is_kept_unkeyed(self) -> None:
        # An entry with neither name nor context cannot be deduped — it is
        # kept verbatim so the per-entry classifier still sees it (fail-closed).
        nameless = {"__typename": "CheckRun", "conclusion": "FAILURE", "status": "COMPLETED"}
        keyed = _check_run(
            "sbom",
            conclusion="SUCCESS",
            started_at="2026-06-19T11:00:00Z",
            completed_at="2026-06-19T11:05:00Z",
        )
        deduped = _dedupe_newest_per_name([keyed, nameless])
        assert nameless in deduped
        assert keyed in deduped
        assert len(deduped) == 2


class TestRequiredContextsVerdictHelper:
    """Defensive branches of ``_required_contexts_verdict`` / ``_check_name``."""

    def test_non_dict_entry_is_ignored(self) -> None:
        # A non-dict deduped entry has no name → not in the required set → ignored.
        assert _check_name("not-a-dict") == ""
        assert _required_contexts_verdict(["not-a-dict", _green("lint")], {"lint"}) == "green"

    def test_worst_verdict_wins_among_entries_sharing_a_required_name(self) -> None:
        # A CheckRun "x" (SUCCESS) and a StatusContext "x" (FAILURE) are distinct
        # identities that both survive dedupe; the required context "x" takes the
        # WORST verdict (failed), never the lenient one.
        entries = [_green("x"), _status_context("x", state="FAILURE", created_at=_T0)]
        assert _required_contexts_verdict(entries, {"x"}) == "failed"


def _contexts_runner(
    *,
    base: str = "main",
    base_rc: int = 0,
    protection: tuple[int, str, str],
    rules: tuple[int, str, str] = _RULES_UNREADABLE,
) -> Callable[[list[str]], tuple[int, str, str]]:
    """A ``gh`` runner scripting the base-branch, rules, then branch-protection calls.

    *rules* defaults to an indeterminate read so a protection-only failure still
    fails closed; the rules-endpoint rescue paths script it explicitly.
    """

    def run(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "baseRefName" in joined:
            return (base_rc, base, "")
        if "rules/branches" in joined:
            return rules
        if "required_status_checks" in joined:
            return protection
        return (0, "", "")

    return run


class TestSharedClassifierHelpers:
    """The #12 SSOT surface consumed by both the keystone and the PR-sweep gate."""

    def test_classify_empty_required_is_green(self) -> None:
        # No required-status-check gate → nothing to satisfy → green.
        assert classify_required_rollup([_failed("eval")], set()) == "green"

    def test_classify_scopes_to_required_and_dedupes(self) -> None:
        # A non-required failing check is ignored; a stale required failure
        # superseded by a newer success does not block.
        rollup = [
            _check_run("test (3.13)", conclusion="FAILURE", started_at=_T0, completed_at=_T1),
            _check_run(
                "test (3.13)",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
            _failed("eval"),
        ]
        assert classify_required_rollup(rollup, {"test (3.13)"}) == "green"

    def test_failing_required_names_is_present_and_failed_only(self) -> None:
        # A failed required check is named; a missing required check (pending) is
        # NOT — the sweep needs to tell "only uv-audit failing" from "still pending".
        rollup = [_failed("uv-audit"), _green("test (3.13)")]
        assert failing_required_names(rollup, {"uv-audit", "test (3.13)", "sbom"}) == {"uv-audit"}

    def test_failing_required_names_dedupes_newest_wins(self) -> None:
        rollup = [
            _check_run(
                "sbom",
                conclusion="SUCCESS",
                started_at="2026-06-19T11:00:00Z",
                completed_at="2026-06-19T11:05:00Z",
            ),
            _check_run("sbom", conclusion="FAILURE", started_at=_T0, completed_at=_T1),
        ]
        assert failing_required_names(rollup, {"sbom"}) == set()

    def test_fetch_required_context_names_returns_set(self) -> None:
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_stub([], required=["lint", "sbom"])):
            assert CodeHostQuery.for_ref(PrRef(slug=_SLUG, pr_id=_PR_ID)).required_context_names() == {"lint", "sbom"}

    def test_fetch_required_context_names_none_on_fetch_failure(self) -> None:
        # Fail CLOSED: an indeterminate required set is signalled as None.
        with patch(
            "teatree.backends.forge_merge_rpc.gh_runner",
            return_value=_gh_stub([], protection_rc=1, protection_body="HTTP 403: Forbidden"),
        ):
            assert CodeHostQuery.for_ref(PrRef(slug=_SLUG, pr_id=_PR_ID)).required_context_names() is None

    def test_fetch_required_context_names_empty_on_no_gate(self) -> None:
        with patch(
            "teatree.backends.forge_merge_rpc.gh_runner",
            return_value=_gh_stub([], protection_rc=1, protection_body="HTTP 404: Branch not protected"),
        ):
            assert CodeHostQuery.for_ref(PrRef(slug=_SLUG, pr_id=_PR_ID)).required_context_names() == set()


class TestRequiredStatusCheckContextsTransport:
    """``GhMergeRpc.fetch_required_status_check_contexts`` — the branch-protection lookup.

    Fail CLOSED (``ROLLUP_QUERY_FAILED`` sentinel) whenever the required set is
    indeterminate; an empty list ONLY for a determinate "no required gate".
    """

    def _contexts(self, runner: Callable[[list[str]], tuple[int, str, str]]) -> list[dict[str, object]]:
        return GhMergeRpc(runner).fetch_required_status_check_contexts(slug=_SLUG, pr_id=_PR_ID)

    def test_union_of_contexts_and_checks_arrays(self) -> None:
        body = json.dumps({"contexts": ["lint", "sbom"], "checks": [{"context": "sbom"}, {"context": "uv-audit"}]})
        result = self._contexts(_contexts_runner(protection=(0, body, "")))
        assert sorted(str(entry["context"]) for entry in result) == ["lint", "sbom", "uv-audit"]

    def test_malformed_context_and_check_entries_are_skipped(self) -> None:
        # Non-str contexts and non-dict / context-less / empty-context checks are
        # defensively ignored; only well-formed required context names survive.
        body = json.dumps(
            {
                "contexts": ["lint", 123, ""],
                "checks": [{"context": "sbom"}, {"context": ""}, {"no_context": 1}, "not-a-dict"],
            },
        )
        result = self._contexts(_contexts_runner(protection=(0, body, "")))
        assert sorted(str(entry["context"]) for entry in result) == ["lint", "sbom"]

    def test_empty_base_branch_fails_closed(self) -> None:
        result = self._contexts(_contexts_runner(base="", protection=(0, "{}", "")))
        assert rollup_query_failed(result)

    def test_base_branch_query_error_fails_closed(self) -> None:
        result = self._contexts(_contexts_runner(base="", base_rc=1, protection=(0, "{}", "")))
        assert rollup_query_failed(result)

    def test_branch_not_protected_404_is_empty_no_gate(self) -> None:
        result = self._contexts(_contexts_runner(protection=(1, "", "HTTP 404: Branch not protected")))
        assert result == []

    def test_required_status_checks_not_enabled_404_is_empty_no_gate(self) -> None:
        result = self._contexts(_contexts_runner(protection=(1, "", "Required status checks not enabled")))
        assert result == []

    def test_generic_404_not_found_is_empty_no_gate(self) -> None:
        # A repo with no branch-protection rule configured at all returns
        # GitHub's generic 404 body — not the "Branch not protected" /
        # "Required status checks not enabled" phrasing (souliane/teatree#2900).
        body = (
            '{"message":"Not Found","documentation_url":'
            '"https://docs.github.com/rest/branches/branch-protection'
            '#get-status-checks-protection","status":"404"}'
        )
        result = self._contexts(_contexts_runner(protection=(1, "", body)))
        assert result == []

    def test_generic_protection_error_fails_closed(self) -> None:
        result = self._contexts(_contexts_runner(protection=(1, "", "HTTP 403: Forbidden")))
        assert rollup_query_failed(result)

    def test_malformed_protection_json_fails_closed(self) -> None:
        result = self._contexts(_contexts_runner(protection=(0, "{not json", "")))
        assert rollup_query_failed(result)

    def test_non_dict_protection_payload_fails_closed(self) -> None:
        result = self._contexts(_contexts_runner(protection=(0, "[1, 2, 3]", "")))
        assert rollup_query_failed(result)

    def test_empty_protection_body_is_empty_required_set(self) -> None:
        # rc==0 with an empty body → no required-status-check rule → no gate.
        result = self._contexts(_contexts_runner(protection=(0, "", "")))
        assert result == []

    _PAT_403 = "HTTP 403: Resource not accessible by personal access token"

    def test_403_protection_rescued_by_rules_endpoint_contexts(self) -> None:
        # The fine-grained-PAT bug: protection 403s, but the rules endpoint IS
        # readable and lists the required contexts — the required set resolves from
        # it (NOT ROLLUP_QUERY_FAILED).
        result = self._contexts(
            _contexts_runner(
                protection=(1, "", self._PAT_403),
                rules=(0, _rules_payload("lint", "sbom"), ""),
            ),
        )
        assert sorted(str(entry["context"]) for entry in result) == ["lint", "sbom"]

    def test_403_protection_and_rules_no_required_rule_is_empty_no_gate(self) -> None:
        # Protection 403, and the readable rules endpoint has NO required_status_checks
        # rule → determinate "no gate" → empty required set (green), not fail-closed.
        result = self._contexts(
            _contexts_runner(
                protection=(1, "", self._PAT_403),
                rules=(0, json.dumps([{"type": "pull_request", "parameters": {}}]), ""),
            ),
        )
        assert result == []

    def test_protection_404_and_no_rules_is_empty_no_gate(self) -> None:
        # Protection determinately unprotected (404) AND the rules endpoint readable
        # with no required rule → determinate no gate → empty required set.
        result = self._contexts(
            _contexts_runner(
                protection=(1, "", "HTTP 404: Branch not protected"),
                rules=(0, "[]", ""),
            ),
        )
        assert result == []

    def test_rules_and_protection_contexts_are_unioned(self) -> None:
        # Both sources readable — the required set is the UNION (each source may
        # carry a name the other omits: classic protection vs a ruleset).
        result = self._contexts(
            _contexts_runner(
                protection=(0, json.dumps({"contexts": ["lint"]}), ""),
                rules=(0, _rules_payload("sbom", "test (3.13)"), ""),
            ),
        )
        assert sorted(str(entry["context"]) for entry in result) == ["lint", "sbom", "test (3.13)"]

    def test_both_endpoints_unreadable_fails_closed(self) -> None:
        # The fail-closed invariant: NEITHER endpoint readable (both 5xx / 403 with
        # no determinate no-gate body) → genuinely indeterminate → refuse.
        result = self._contexts(
            _contexts_runner(
                protection=(1, "", self._PAT_403),
                rules=(1, "", "HTTP 500: server error"),
            ),
        )
        assert rollup_query_failed(result)

    def test_unparseable_rules_falls_back_to_protection(self) -> None:
        # The rules endpoint returns garbage (indeterminate for that source), but
        # protection is readable → the required set still resolves from protection.
        result = self._contexts(
            _contexts_runner(
                protection=(0, json.dumps({"contexts": ["lint"]}), ""),
                rules=(0, "{not json", ""),
            ),
        )
        assert sorted(str(entry["context"]) for entry in result) == ["lint"]


class TestChangedPathsTransport:
    """``GhMergeRpc.fetch_pr_changed_paths`` — paginated diff read, fail-closed sentinel."""

    def test_paginated_files_are_returned(self) -> None:
        # ``--paginate`` follows every page; the jq emits one filename per line.
        runner = _seq_runner([(0, "a.py\nsrc/teatree/core/merge/x.py\n", "")])
        assert GhMergeRpc(runner).fetch_pr_changed_paths(slug=_SLUG, pr_id=_PR_ID) == [
            "a.py",
            "src/teatree/core/merge/x.py",
        ]
        assert runner.calls[0] == ["api", "--paginate", f"repos/{_SLUG}/pulls/{_PR_ID}/files", "--jq", ".[].filename"]

    def test_fetch_error_returns_unavailable_sentinel(self) -> None:
        runner = _seq_runner([(1, "", "HTTP 502")])
        result = GhMergeRpc(runner).fetch_pr_changed_paths(slug=_SLUG, pr_id=_PR_ID)
        assert changed_paths_unavailable(result)


class TestAttachTouchedPaths:
    """``attach_touched_paths`` fails CLOSED to substrate on an unreadable/truncated diff."""

    def _logic_clear(self) -> MergeClear:
        return MergeClear(
            pr_id=_PR_ID,
            slug=_SLUG,
            reviewed_sha="a" * 40,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

    def test_complete_paths_populate_touched_paths(self) -> None:
        clear = self._logic_clear()
        attach_touched_paths(clear, _StubQuery(["a.py", "b.py"]))
        assert clear.touched_paths == ("a.py", "b.py")
        assert clear.substrate_paths_indeterminate is False
        assert clear.is_substrate() is False

    def test_unavailable_sentinel_holds_as_substrate(self) -> None:
        clear = self._logic_clear()
        attach_touched_paths(clear, _StubQuery([CHANGED_PATHS_UNAVAILABLE]))
        assert clear.substrate_paths_indeterminate is True
        assert clear.is_substrate() is True

    def test_fetch_exception_holds_as_substrate(self) -> None:
        clear = self._logic_clear()
        attach_touched_paths(clear, _StubQuery([], raises=True))
        assert clear.substrate_paths_indeterminate is True
        assert clear.is_substrate() is True


class TestExpectedRequiredContextsFloor(TestCase):
    """The operator floor fails a DETERMINATE-EMPTY required set closed (Medium finding)."""

    def test_empty_required_with_no_floor_is_green(self) -> None:
        # No floor configured (default): a gate-less repo still merges green.
        assert _verdict([_failed("eval")], required=[]) == "green"

    def test_empty_required_with_floor_fails_closed(self) -> None:
        # A floor is configured but the forge reports NO required checks (branch
        # protection removed/absent) → fail closed, never "all checks passed".
        call_command("config_setting", "set", "expected_required_contexts", '["test (3.13)"]')
        assert _verdict([_failed("eval")], required=[]) == "failed"

    def test_present_required_set_is_unaffected_by_floor(self) -> None:
        # The floor only bites the empty case; a determinate non-empty required set
        # classifies normally.
        call_command("config_setting", "set", "expected_required_contexts", '["test (3.13)"]')
        assert _verdict(_all_required_green(), required=_REQUIRED) == "green"

    def test_default_floor_is_determinate_empty_set(self) -> None:
        # The default config resolves to a determinate-EMPTY floor (``set()``), NOT
        # ``None`` — so a genuinely gate-less repo still merges green.
        assert _expected_required_contexts_floor() == set()


class TestFloorUnreadableFailsClosed:
    """F2.4: an UNREADABLE floor is indeterminate → fail CLOSED, never silent 'no floor'."""

    def test_floor_read_exception_returns_none(self) -> None:
        # A config-read failure must be reported as ``None`` (indeterminate), never
        # swallowed to an empty set (which would read as "no floor configured").
        with patch("teatree.config.get_effective_settings", side_effect=RuntimeError("config store down")):
            assert _expected_required_contexts_floor() is None

    def test_empty_required_with_unreadable_floor_fails_closed(self) -> None:
        # The keystone verdict: a repo reporting NO required checks while the floor
        # could not be read is indeterminate → REFUSED, never green-on-empty.
        with patch("teatree.core.merge.ci_rollup._expected_required_contexts_floor", return_value=None):
            assert _verdict([_failed("eval")], required=[]) == "failed"

    def test_present_required_set_unaffected_when_floor_unreadable(self) -> None:
        # An unreadable floor only bites the empty-required case; a determinate
        # non-empty required set still classifies on its own merits.
        with patch("teatree.core.merge.ci_rollup._expected_required_contexts_floor", return_value=None):
            assert _verdict(_all_required_green(), required=_REQUIRED) == "green"


class TestGitlabPipelineClassification:
    """F2.1: ONLY ``success`` is green; ``manual`` / ``skipped`` are pending (not-passed CI)."""

    def test_success_is_green(self) -> None:
        assert _classify_gitlab_pipeline("success") == "green"

    def test_manual_is_pending_not_green(self) -> None:
        # A blocked-manual pipeline has NOT run its later required stages — it must
        # never merge a keystone MR as "all checks passed".
        assert _classify_gitlab_pipeline("manual") == "pending"

    def test_skipped_is_pending_not_green(self) -> None:
        # A skipped required pipeline never ran — pending, not green.
        assert _classify_gitlab_pipeline("skipped") == "pending"

    def test_running_is_pending(self) -> None:
        assert _classify_gitlab_pipeline("running") == "pending"

    def test_failed_is_failed(self) -> None:
        assert _classify_gitlab_pipeline("failed") == "failed"

    def test_canceled_is_failed(self) -> None:
        assert _classify_gitlab_pipeline("canceled") == "failed"

    def test_case_insensitive_manual_is_pending(self) -> None:
        # The classifier lower-cases first, so a MANUAL/Manual status is still pending.
        assert _classify_gitlab_pipeline("MANUAL") == "pending"
        assert _classify_gitlab_pipeline("Skipped") == "pending"


class TestRollupEntryTypename:
    """F2.9: ``__typename`` is part of the dedupe identity (functional-TypedDict key)."""

    def test_check_identity_reads_typename(self) -> None:
        checkrun = _check_run("x", started_at=_T0, completed_at=_T1)
        status = _status_context("x", state="SUCCESS", created_at=_T0)
        # Same NAME, different __typename → distinct identities (the key reads it).
        assert _check_identity(checkrun) == ("CheckRun", "x")
        assert _check_identity(status) == ("StatusContext", "x")
        assert _check_identity(checkrun) != _check_identity(status)

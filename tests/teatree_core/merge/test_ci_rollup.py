"""GitHub required-checks rollup classification (BLUEPRINT §17.4.3 step 3).

``fetch_required_checks_status`` re-verifies the live required-checks at merge
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

from teatree.backends.forge_merge_rpc import GhMergeRpc
from teatree.core.backend_protocols import rollup_query_failed
from teatree.core.merge import fetch_required_checks_status
from teatree.core.merge.ci_rollup import _check_name, _dedupe_newest_per_name, _required_contexts_verdict

_SLUG = "souliane/teatree"
_PR_ID = 2580


def _rollup_names(rollup: list[dict[str, object]]) -> list[str]:
    """The distinct check names in *rollup* (CheckRun ``name`` / StatusContext ``context``)."""
    names: list[str] = []
    for entry in rollup:
        if isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("context") or "")
            if name and name not in names:
                names.append(name)
    return names


def _gh_stub(
    rollup: list[dict[str, object]],
    *,
    required: list[str] | None = None,
    protection_rc: int = 0,
    protection_body: str | None = None,
) -> Callable[[list[str]], tuple[int, str, str]]:
    """A ``gh`` runner answering the statusCheckRollup AND branch-protection queries.

    *required* is the branch-protection ``required_status_checks`` context set the
    repo reports. When ``None`` it defaults to every name present in *rollup* — so
    the dedupe tests, which treat all checks as required, keep their semantics.
    *protection_rc* / *protection_body* simulate a failed (or "no protection")
    branch-protection fetch — the fail-closed / no-gate paths.
    """
    contexts = _rollup_names(rollup) if required is None else required

    def run(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "statusCheckRollup" in joined:
            return (0, json.dumps(rollup), "")
        if "baseRefName" in joined:
            return (0, "main", "")
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
) -> str:
    stub = _gh_stub(rollup, required=required, protection_rc=protection_rc, protection_body=protection_body)
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub):
        return fetch_required_checks_status(_SLUG, _PR_ID, host_kind="github")


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
    not reach the helper through the public ``fetch_required_checks_status``
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
) -> Callable[[list[str]], tuple[int, str, str]]:
    """A ``gh`` runner scripting the base-branch then branch-protection calls."""

    def run(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "baseRefName" in joined:
            return (base_rc, base, "")
        if "required_status_checks" in joined:
            return protection
        return (0, "", "")

    return run


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

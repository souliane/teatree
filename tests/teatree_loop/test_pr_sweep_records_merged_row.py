# test-path: cross-cutting — drives the sweep (loop.scanners.pr_sweep*) through the
# real merge chokepoint (core.merge, core.models.pull_request/review_verdict); no
# single src/teatree/ mirror dir covers the join.
"""The sweep's own merge must leave its ledger row reading ``merged`` (#3984).

Every other sweep test stubs :meth:`PrApiClient.merge_pr_squash_bound` outright, so
none of them can observe what the production merge path writes — the ledger row stayed
``open`` for 32 of 33 rows while the whole suite was green. These tests drive the sweep
through the REAL adapter (``GhPrApiClient.merge_pr_squash_bound`` →
``execute_bound_merge``) with only the ``gh`` transport stubbed, so they fail if the
recorder is removed from the merge chokepoint.

Both no-CLEAR sweep merge routes are covered: the solo-overlay bypass and the
uv-audit fallback that raw-merges when the keystone refuses on that same path.
"""

import json
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from teatree.core.models import ImplementedIssueMarker, PullRequest, Ticket
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.loop.scanners.pr_sweep import PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient, NullMergeNotifier
from teatree.loop.scanners.pr_sweep_ports import MergeKeystone, PrApiClient
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
PR_ID = 6230
PR_URL = f"https://github.com/{SLUG}/pull/{PR_ID}"
ISSUE_URL = "https://github.com/souliane/teatree/issues/3984"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
MERGED_SHA = "abcdef1234567890abcdef1234567890abcdef12"


@pytest.fixture(autouse=True)
def _non_substrate_diff():
    with patch(
        "teatree.core.merge.ci_rollup.CodeHostQuery.pr_changed_paths",
        return_value=["src/teatree/loop/scanners/pr_sweep.py"],
    ):
        yield


@pytest.fixture(autouse=True)
def _repo_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.core.review.author_trust.repo_is_internal", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _required_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teatree.core.merge.ci_rollup.CodeHostQuery.required_context_names",
        lambda *a, **k: {"test (3.13)"},
    )


def _green_check(name: str = "test (3.13)", *, conclusion: str = "SUCCESS") -> RawAPIDict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "startedAt": "2026-08-01T10:00:00Z",
        "completedAt": "2026-08-01T10:05:00Z",
    }


def _open_pr(*, checks: tuple[RawAPIDict, ...] = ()) -> PrSummary:
    return PrSummary(
        slug=SLUG,
        number=PR_ID,
        head_sha=HEAD,
        is_draft=False,
        has_changes_requested=False,
        rollup=checks or (_green_check(),),
        url=PR_URL,
        title=f"PR {PR_ID}",
        behind_main=False,
        author="souliane",
        same_repo=None,
    )


@dataclass(slots=True)
class _SweepApi:
    """The production merge adapter, with only the two read calls scripted.

    ``merge_pr_squash_bound`` is delegated VERBATIM to :class:`GhPrApiClient`, which is
    what makes this an end-to-end exercise of the sweep's merge path rather than a
    restatement of a stub's return value.
    """

    prs: tuple[PrSummary, ...]
    main_uv_audit_red: bool = False
    merge_calls: list[tuple[str, int, str]] = field(default_factory=list)

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        return [pr for pr in self.prs if pr.slug == slug]

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        return self.main_uv_audit_red

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:
        self.merge_calls.append((slug, pr_id, expected_head_oid))
        return GhPrApiClient(token="").merge_pr_squash_bound(
            slug=slug, pr_id=pr_id, expected_head_oid=expected_head_oid
        )


@dataclass(slots=True)
class _RefusingKeystone:
    """A keystone that refuses every CLEAR — the uv-audit fallback's precondition."""

    calls: list[int] = field(default_factory=list)

    def merge_clear(self, *, clear_id: int, human_authorized: str = "") -> tuple[bool, str, str, str, str]:
        self.calls.append(clear_id)
        return False, "", "keystone_refused", "", ""


class _GhStub:
    """Scripted ``gh`` responses for the merge chokepoint's live re-reads."""

    def __init__(self, *, checks: str) -> None:
        self.answers = {
            "baseRefName": "main",
            "required_status_checks": json.dumps({"contexts": []}),
            "headRefOid": HEAD,
            "isDraft": "false",
            "statusCheckRollup": checks,
        }

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        for probe, answer in self.answers.items():
            if probe in joined:
                return (0, answer, "")
        if "pulls" in joined and "merge" in joined:
            return (0, json.dumps({"sha": MERGED_SHA}), "")
        return (0, "", "")


def _seed_ledger() -> tuple[Ticket, PullRequest]:
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW, issue_url=ISSUE_URL)
    row = PullRequest.objects.create(
        ticket=ticket,
        overlay="t3-teatree",
        url=PR_URL,
        repo=SLUG,
        iid=str(PR_ID),
    )
    assert row.state == PullRequest.State.OPEN
    return ticket, row


def _record_cold_review() -> None:
    ReviewVerdict.record(
        pr_id=PR_ID,
        slug=SLUG,
        reviewed_sha=HEAD,
        verdict="merge_safe",
        reviewer_identity="cold-reviewer",
    )


def _sweep(api: PrApiClient, keystone: MergeKeystone, *, gh: _GhStub) -> list[str]:
    scanner = PrSweepScanner(
        repos=(SLUG,),
        api=api,
        keystone=keystone,
        notifier=NullMergeNotifier(),
        overlay="t3-teatree",
        solo_overlay=True,
        self_identities=("souliane",),
    )
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=gh):
        return [signal.kind for signal in scanner.scan()]


class TestSoloOverlayBypassRecordsTheMerge:
    def test_sweep_merged_pr_leaves_its_ledger_row_merged(self) -> None:
        _, row = _seed_ledger()
        _record_cold_review()
        api = _SweepApi(prs=(_open_pr(),))

        kinds = _sweep(api, _RefusingKeystone(), gh=_GhStub(checks=json.dumps([{"conclusion": "SUCCESS"}])))

        assert kinds == ["pr_sweep.merged", "pr_sweep.pass"]
        assert api.merge_calls == [(SLUG, PR_ID, HEAD)]
        row.refresh_from_db()
        assert row.state == PullRequest.State.MERGED

    def test_release_rule_frees_the_budget_slot_for_a_sweep_merged_pr(self) -> None:
        """The whole point of the row write: intake releases with no operator action."""
        _seed_ledger()
        _record_cold_review()
        marker = ImplementedIssueMarker.objects.create(issue_url=ISSUE_URL, overlay="t3-teatree")
        assert ImplementedIssueMarker.objects.find_stale("t3-teatree").completed == ()

        _sweep(
            _SweepApi(prs=(_open_pr(),)),
            _RefusingKeystone(),
            gh=_GhStub(checks=json.dumps([{"conclusion": "SUCCESS"}])),
        )

        assert ImplementedIssueMarker.objects.find_stale("t3-teatree").completed == (marker.pk,)


class TestUvAuditFallbackRecordsTheMerge:
    def test_raw_fallback_merge_after_a_keystone_refusal_stamps_the_row(self) -> None:
        from teatree.core.models.merge_clear import ClearRequest, MergeClear  # noqa: PLC0415 — test-local

        _, row = _seed_ledger()
        _record_cold_review()
        MergeClear.issue(
            ClearRequest(
                pr_id=PR_ID,
                slug=SLUG,
                reviewed_sha=HEAD,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
            )
        )
        api = _SweepApi(
            prs=(_open_pr(checks=(_green_check(), _green_check("uv-audit", conclusion="FAILURE"))),),
            main_uv_audit_red=True,
        )
        keystone = _RefusingKeystone()

        with patch(
            "teatree.core.merge.ci_rollup.CodeHostQuery.required_context_names",
            return_value={"test (3.13)", "uv-audit"},
        ):
            kinds = _sweep(api, keystone, gh=_GhStub(checks=json.dumps([{"conclusion": "SUCCESS"}])))

        assert keystone.calls != []
        assert kinds == ["pr_sweep.merged", "pr_sweep.pass"]
        row.refresh_from_db()
        assert row.state == PullRequest.State.MERGED

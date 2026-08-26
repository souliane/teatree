"""The PR sweep reads each repo on the forge that repo actually lives on (#72).

The sweep shelled out to the GitHub CLI for every slug. A bare ``owner/repo``
carries no host, so on a GitLab project that call authenticated against the wrong
forge, raised, and was caught and logged — the sweep reported success having
enumerated nothing. These pin the three halves of the fix: the slug is routed to
the arm its declared forge speaks, the GitLab arm's own CI gate is read (GitLab
answers no branch-protection required set, which the GitHub ladder reads as
GREEN), and every unanswerable read is a ``ScannerError`` rather than an empty
list a caller reads as "nothing to merge".

Only the unstoppable externals are stubbed — the forge transport and the overlay
registry that declares which host owns a namespace.
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.test import TestCase

from teatree.contrib.t3_teatree.overlay import TeatreeMetadata
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models.merge_clear import ClearRequest, MergeClear
from teatree.core.overlay import OverlayBase, OverlayConfig, OverlayMetadata
from teatree.loop.scanner_factories import _pr_sweep_scanner_for
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.pr_sweep import PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import NullMergeNotifier
from teatree.loop.scanners.pr_sweep_gitlab import ForgePrApiClient, GlabPrApiClient
from teatree.loop.sweep_on_demand import trigger_sweep_for_verdict
from teatree.types import RawAPIDict

pytestmark = pytest.mark.django_db  # ast-grep-ignore: ac-django-no-pytest-django-db

_GITLAB_SLUG = "acme-eng/platform/widget-api"
_GITHUB_SLUG = "souliane/teatree"
_HEAD = "b" * 40
_MERGED = "c" * 40
_MR_IID = 4120

_OWNED_BY_HOST = {
    "gitlab.com": ["acme-eng"],
    "github.com": ["souliane"],
}


def _declaring_both_forges() -> dict[str, SimpleNamespace]:
    """One overlay declaring where each test namespace is hosted (the SCOPE axis)."""
    return {"acme": SimpleNamespace(config=SimpleNamespace(owned_repos=_OWNED_BY_HOST))}


@contextmanager
def _declared_scopes(owned: dict[str, list[str]] | None = None) -> Iterator[None]:
    """Stub the two host-bearing sources ``forge_for_repo_slug`` reads."""
    overlays = (
        _declaring_both_forges()
        if owned is None
        else {"acme": SimpleNamespace(config=SimpleNamespace(owned_repos=owned))}
    )
    with ExitStack() as stack:
        stack.enter_context(patch("teatree.core.merge.host_kind.find_project_root", return_value=None))
        stack.enter_context(patch("teatree.core.merge.host_kind.get_all_overlays", return_value=overlays))
        yield


def _mr(**overrides: object) -> RawAPIDict:
    """A GitLab ``merge_requests`` list entry, in the shape the sweep decodes."""
    payload: RawAPIDict = {
        "iid": _MR_IID,
        "sha": _HEAD,
        "title": "fix(sweep): something",
        "web_url": f"https://gitlab.com/{_GITLAB_SLUG}/-/merge_requests/{_MR_IID}",
        "draft": False,
        "author": {"username": "souliane"},
        "source_project_id": 77,
        "target_project_id": 77,
        "has_conflicts": False,
        "detailed_merge_status": "mergeable",
        "blocking_discussions_resolved": True,
    }
    return payload | overrides


@dataclass(slots=True)
class RecordingArm:
    """A ``PrApiClient`` that records which slugs reached it."""

    listed: list[str] = field(default_factory=list)
    merged: list[tuple[str, int]] = field(default_factory=list)
    prs: list[PrSummary] = field(default_factory=list)

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        self.listed.append(slug)
        return list(self.prs)

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        del slug, check_name
        return False

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:
        del expected_head_oid
        self.merged.append((slug, pr_id))
        return True, _MERGED

    def update_pr_branch(self, *, slug: str, pr_id: int, expected_head_oid: str) -> bool:
        del slug, pr_id, expected_head_oid
        return True


@dataclass(slots=True)
class FakeCodeHost:
    """The GitLab ``CodeHostBackend`` reads ``GlabPrApiClient`` makes."""

    repo: RawAPIDict = field(default_factory=lambda: {"id": 77})
    mrs: list[RawAPIDict] = field(default_factory=list)
    raises: Exception | None = None

    def get_repo(self, *, repo: str) -> RawAPIDict:
        del repo
        if self.raises is not None:
            raise self.raises
        return dict(self.repo)

    def list_prs(self, *, repo: str, state: str = "", author: str = "") -> list[RawAPIDict]:
        del repo, state, author
        return list(self.mrs)


@dataclass(slots=True)
class FakeKeystone:
    """The §17.4 merge transition — records the CLEARs the sweep drove."""

    calls: list[int] = field(default_factory=list)

    def merge_clear(self, *, clear_id: int, human_authorized: str = "") -> tuple[bool, str, str, str, str]:
        del human_authorized
        self.calls.append(clear_id)
        return True, _MERGED, "", "", ""


def _gitlab_pr(**overrides: object) -> PrSummary:
    base = PrSummary(
        slug=_GITLAB_SLUG,
        number=_MR_IID,
        head_sha=_HEAD,
        is_draft=False,
        has_changes_requested=False,
        url=f"https://gitlab.com/{_GITLAB_SLUG}/-/merge_requests/{_MR_IID}",
        title="fix(sweep): something",
        author="souliane",
        same_repo=True,
        host_kind="gitlab",
    )
    return replace(base, **overrides) if overrides else base


def _issue_clear() -> MergeClear:
    return MergeClear.issue(
        ClearRequest(
            pr_id=_MR_IID,
            slug=_GITLAB_SLUG,
            reviewed_sha=_HEAD,
            reviewer_identity="cold-reviewer",
            gh_verify_result="green",
            blast_class="logic",
            host_kind="gitlab",
        )
    )


class TestForgeRouting(TestCase):
    """A slug reaches the arm its own declared forge speaks — never the other one."""

    def test_a_gitlab_project_is_listed_through_the_gitlab_arm(self) -> None:
        github, gitlab = RecordingArm(), RecordingArm()
        client = ForgePrApiClient(github=github, gitlab=gitlab)
        with _declared_scopes():
            client.list_open_prs(slug=_GITLAB_SLUG)
        assert gitlab.listed == [_GITLAB_SLUG]
        assert github.listed == []

    def test_a_github_repo_still_reaches_the_github_arm(self) -> None:
        github, gitlab = RecordingArm(), RecordingArm()
        client = ForgePrApiClient(github=github, gitlab=gitlab)
        with _declared_scopes():
            client.list_open_prs(slug=_GITHUB_SLUG)
        assert github.listed == [_GITHUB_SLUG]
        assert gitlab.listed == []

    def test_the_bound_merge_routes_on_the_same_declaration(self) -> None:
        github, gitlab = RecordingArm(), RecordingArm()
        client = ForgePrApiClient(github=github, gitlab=gitlab)
        with _declared_scopes():
            client.merge_pr_squash_bound(slug=_GITLAB_SLUG, pr_id=_MR_IID, expected_head_oid=_HEAD)
        assert gitlab.merged == [(_GITLAB_SLUG, _MR_IID)]
        assert github.merged == []

    def test_an_undeclared_namespace_raises_instead_of_sweeping_it_on_a_guess(self) -> None:
        client = ForgePrApiClient(github=RecordingArm(), gitlab=RecordingArm())
        with _declared_scopes({"github.com": ["someone-else"]}), pytest.raises(ScannerError) as caught:
            client.list_open_prs(slug=_GITLAB_SLUG)
        assert "no forge is declared" in caught.value.detail

    def test_an_ambiguous_declaration_refuses_rather_than_picking_one(self) -> None:
        client = ForgePrApiClient(github=RecordingArm(), gitlab=RecordingArm())
        contested = {"gitlab.com": ["acme-eng"], "github.com": ["acme-eng"]}
        with _declared_scopes(contested), pytest.raises(ScannerError):
            client.list_open_prs(slug=_GITLAB_SLUG)


class TestGitLabListIsLoudNotEmpty(TestCase):
    """An unreadable project is an error; only a RESOLVED project answers "none"."""

    def test_an_unresolvable_project_raises_instead_of_reporting_no_open_mrs(self) -> None:
        host = FakeCodeHost(repo={"error": "Could not resolve project: acme-eng/platform/widget-api"})
        with patch.object(GlabPrApiClient, "_backend", return_value=host), pytest.raises(ScannerError) as caught:
            GlabPrApiClient().list_open_prs(slug=_GITLAB_SLUG)
        assert caught.value.error_class is ScannerErrorClass.AUTH

    def test_a_resolved_project_with_no_open_mrs_is_a_plain_empty_list(self) -> None:
        with patch.object(GlabPrApiClient, "_backend", return_value=FakeCodeHost(mrs=[])):
            assert GlabPrApiClient().list_open_prs(slug=_GITLAB_SLUG) == []

    def test_a_401_is_reported_as_an_auth_failure(self) -> None:
        response = httpx.Response(401, request=httpx.Request("GET", "https://gitlab.com"))
        host = FakeCodeHost(raises=httpx.HTTPStatusError("unauthorized", request=response.request, response=response))
        with patch.object(GlabPrApiClient, "_backend", return_value=host), pytest.raises(ScannerError) as caught:
            GlabPrApiClient().list_open_prs(slug=_GITLAB_SLUG)
        assert caught.value.error_class is ScannerErrorClass.AUTH

    def test_an_offline_box_is_reported_as_a_network_failure(self) -> None:
        host = FakeCodeHost(raises=httpx.ConnectError("no route to host"))
        with patch.object(GlabPrApiClient, "_backend", return_value=host), pytest.raises(ScannerError) as caught:
            GlabPrApiClient().list_open_prs(slug=_GITLAB_SLUG)
        assert caught.value.error_class is ScannerErrorClass.NETWORK


class TestGitLabMrDecoding(TestCase):
    """The MR payload maps onto the forge-neutral summary the decision ladder reads."""

    @staticmethod
    def _decode(**overrides: object) -> PrSummary:
        with patch.object(GlabPrApiClient, "_backend", return_value=FakeCodeHost(mrs=[_mr(**overrides)])):
            return GlabPrApiClient().list_open_prs(slug=_GITLAB_SLUG)[0]

    def test_an_open_mr_carries_its_iid_head_author_and_forge(self) -> None:
        summary = self._decode()
        assert (summary.number, summary.head_sha, summary.author) == (_MR_IID, _HEAD, "souliane")
        assert summary.host_kind == "gitlab"
        assert summary.same_repo is True

    def test_the_legacy_wip_flag_still_reads_as_a_draft(self) -> None:
        assert self._decode(draft=False, work_in_progress=True).is_draft is True

    def test_an_unresolved_blocking_thread_reads_as_changes_requested(self) -> None:
        assert self._decode(blocking_discussions_resolved=False).has_changes_requested is True

    def test_a_settled_conflict_is_flagged(self) -> None:
        assert self._decode(has_conflicts=True, detailed_merge_status="conflict").is_conflicted is True

    def test_a_still_checking_mergeability_is_not_a_conflict(self) -> None:
        assert self._decode(has_conflicts=True, detailed_merge_status="checking").is_conflicted is False

    def test_a_fork_head_reports_cross_repo_provenance(self) -> None:
        assert self._decode(source_project_id=99).same_repo is False

    def test_an_unreported_provenance_stays_indeterminate(self) -> None:
        assert self._decode(source_project_id=None, target_project_id=None).same_repo is None


class TestGitLabCiGate(TestCase):
    """GitLab's verdict is the head pipeline — the GitHub ladder reads it as GREEN."""

    def _sweep(self, *, pipeline: str) -> tuple[list[str], FakeKeystone]:
        keystone = FakeKeystone()
        arm = RecordingArm(prs=[_gitlab_pr()])
        scanner = PrSweepScanner(
            repos=(_GITLAB_SLUG,),
            api=ForgePrApiClient(github=RecordingArm(), gitlab=arm),
            keystone=keystone,
            notifier=NullMergeNotifier(),
            overlay="acme",
            self_identities=("souliane",),
        )
        with (
            _declared_scopes(),
            # What GitLab's own backend answers: no branch-protection required set.
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.required_context_names", return_value=set()),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.required_checks_status", return_value=pipeline),
            patch("teatree.core.review.author_trust.repo_is_internal", return_value=True),
        ):
            signals = scanner.scan()
        return [str(signal.payload.get("reason") or signal.payload.get("decision")) for signal in signals], keystone

    def test_a_failed_pipeline_blocks_the_merge(self) -> None:
        _issue_clear()
        reasons, keystone = self._sweep(pipeline="failed")
        assert keystone.calls == []
        assert "ci_red" in reasons

    def test_a_running_pipeline_holds_the_merge(self) -> None:
        _issue_clear()
        reasons, keystone = self._sweep(pipeline="pending")
        assert keystone.calls == []
        assert "ci_pending" in reasons

    def test_a_green_pipeline_drives_the_cleared_merge(self) -> None:
        clear = _issue_clear()
        _reasons, keystone = self._sweep(pipeline="green")
        assert keystone.calls == [clear.pk]


class TestSweepFactoryWiring(TestCase):
    """The scanner the loop builds is the forge router, not the GitHub client."""

    @pytest.fixture(autouse=True)
    def _hermetic_overlay_discovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])

    @staticmethod
    def _backend() -> OverlayBackends:
        config = MagicMock(spec=OverlayConfig)
        config.get_github_token = lambda: "gh-token"
        config.get_gitlab_token = lambda: "glab-token"
        metadata = MagicMock(spec=OverlayMetadata)
        metadata.get_followup_repos = lambda: [_GITLAB_SLUG]
        overlay = MagicMock(spec=OverlayBase)
        overlay.config, overlay.metadata = config, metadata
        return OverlayBackends(
            name="acme",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=(),
            overlay=overlay,
            identities=(),
        )

    def test_the_built_sweep_carries_both_forge_arms(self) -> None:
        scanner = _pr_sweep_scanner_for(self._backend(), slack_user_id="")
        assert scanner is not None
        assert isinstance(scanner.api, ForgePrApiClient)
        assert isinstance(scanner.api.gitlab, GlabPrApiClient)

    def test_each_arm_authenticates_under_its_own_overlay_credential(self) -> None:
        scanner = _pr_sweep_scanner_for(self._backend(), slack_user_id="")
        assert scanner is not None
        assert isinstance(scanner.api, ForgePrApiClient)
        assert scanner.api.gitlab.token == "glab-token"


class TestOnDemandSweepSurfacesTheFailure(TestCase):
    """``review record`` reports success either way — so the sweep's failure must DM."""

    def test_an_unreadable_forge_reaches_the_owner_notice(self) -> None:
        error = ScannerError(scanner="pr_sweep", error_class=ScannerErrorClass.AUTH, detail="401 on the project")
        scanner = SimpleNamespace(evaluate_one=lambda **_kw: (_ for _ in ()).throw(error))
        with (
            patch("teatree.loop.sweep_on_demand._sweep_scanner_for_overlay", return_value=scanner),
            patch("teatree.loop.sweep_on_demand.notify_scanner_error") as notice,
        ):
            assert trigger_sweep_for_verdict(slug=_GITLAB_SLUG, pr_id=_MR_IID, overlay="acme") is None
        assert notice.call_args.kwargs["exc"] is error


class TestFollowupRepoPaths(TestCase):
    """The sweep's repo list keeps every declared forge path, nested groups included."""

    @staticmethod
    def _followups(*workspace_repos: str) -> list[str]:
        config = SimpleNamespace(workspace_repos=list(workspace_repos))
        return TeatreeMetadata(cast("OverlayConfig", config)).get_followup_repos()

    def test_a_nested_gitlab_project_path_is_swept(self) -> None:
        assert self._followups(_GITLAB_SLUG) == [_GITLAB_SLUG]

    def test_a_two_segment_slug_is_still_swept(self) -> None:
        assert self._followups(_GITHUB_SLUG) == [_GITHUB_SLUG]

    def test_a_bare_directory_name_is_not_a_repo_path(self) -> None:
        assert self._followups("widget-api") == ["souliane/teatree"]

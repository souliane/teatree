"""The forge-transport resolution a CLEAR's merge binds to (``core.merge.host_kind``).

A bare ``owner/repo`` slug carries no host, so the forge has to come from the
CLEAR's own recorded target or from host-bearing evidence. The rule these tests
pin is that there is NO default: the old ``forge_from_remote(issue_url) or
"github"`` bound the GitHub transport against a GitLab MR whenever the CLEAR had
no ticket, which made a managed-repo MR with no teatree Ticket unmergeable
through the sanctioned path while still leaving an unconsumed ``MergeClear``
orphan behind.

Only the unstoppable externals — the ``glab``/``gh`` subprocess and the local
clone's git remote — are stubbed; every teatree model / FSM / DB write is real.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import DraftState
from teatree.core.merge import merge_ticket_pr
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.host_kind import resolve_host_kind
from teatree.core.models import ClearIssuanceError, ClearRequest, MergeAudit, MergeClear, Ticket
from tests.teatree_core.conftest import seed_merge_safe_verdict

pytestmark = pytest.mark.django_db  # ast-grep-ignore: ac-django-no-pytest-django-db

_SHA = "d" * 40
_GITLAB_SLUG = "acme-eng/widget-api"
_MR_IID = 6264
_DRAFT_PROBE = "teatree.core.merge.ci_rollup.CodeHostQuery.pr_draft_state"


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The #1773/#3244 provenance gate is exercised by its own suites; these
    # tests target the transport resolution that runs before it.
    monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)


def _overlay_owning(owned_repos: dict[str, list[str]]) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(owned_repos=owned_repos))


def _ticketless_clear(**overrides: object) -> MergeClear:
    defaults: dict[str, object] = {
        "ticket": None,
        "pr_id": _MR_IID,
        "slug": _GITLAB_SLUG,
        "reviewed_sha": _SHA,
        "reviewer_identity": "cold-reviewer",
        "gh_verify_result": MergeClear.VerifyResult.GREEN,
        "blast_class": MergeClear.BlastClass.LOGIC,
    }
    defaults.update(overrides)
    return MergeClear.objects.create(**defaults)


class _GlabStub:
    """Scripted ``glab`` responses for the MR under test; records argv per call."""

    def __init__(self, *, merge_sha: str = "glab-merged-sha") -> None:
        self.merge_sha = merge_sha
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        if "/merge" in joined and "PUT" in argv:
            return (0, json.dumps({"state": "merged", "merge_commit_sha": self.merge_sha}), "")
        if "/pipelines" in joined:
            return (0, json.dumps([{"id": 1, "status": "success", "sha": _SHA}]), "")
        if "/merge_requests/" in joined:
            return (0, json.dumps({"iid": _MR_IID, "sha": _SHA, "draft": False, "state": "opened"}), "")
        return (0, "", "")


class TestResolveHostKind(TestCase):
    def test_recorded_forge_resolves_a_ticketless_clear(self) -> None:
        clear = _ticketless_clear(host_kind="gitlab")
        assert resolve_host_kind(clear, repo_slug=_GITLAB_SLUG) == "gitlab"

    def test_recorded_forge_outranks_the_ticket_issue_url(self) -> None:
        # An explicit target is the operator's statement about WHERE the MR
        # lives; a ticket filed on another forge must not override it.
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://github.com/souliane/teatree/issues/1")
        clear = _ticketless_clear(ticket=ticket, host_kind="gitlab")
        assert resolve_host_kind(clear, repo_slug=_GITLAB_SLUG) == "gitlab"

    def test_an_unknown_recorded_forge_raises_rather_than_deriving_around_it(self) -> None:
        # Deriving past a typo would bind a forge the caller never named — the
        # instruction must fail loud, not be silently discarded.
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://github.com/souliane/teatree/issues/1")
        clear = _ticketless_clear(ticket=ticket, host_kind="bitbucket")
        with pytest.raises(MergePreconditionError, match="unknown forge 'bitbucket'"):
            resolve_host_kind(clear, repo_slug=_GITLAB_SLUG)

    def test_ticket_issue_url_resolves_when_no_forge_recorded(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab.example.com/acme-eng/widget-api/-/issues/9",
        )
        assert resolve_host_kind(_ticketless_clear(ticket=ticket), repo_slug=_GITLAB_SLUG) == "gitlab"

    def test_running_clone_origin_resolves_its_own_repo(self) -> None:
        clear = _ticketless_clear(slug="souliane/teatree")
        with (
            patch("teatree.core.merge.host_kind.find_project_root", return_value=Path("/tmp/clone")),
            patch(
                "teatree.core.merge.host_kind.remote_url",
                return_value="git@github.com:souliane/teatree.git",
            ),
        ):
            assert resolve_host_kind(clear, repo_slug="souliane/teatree") == "github"

    def test_running_clone_origin_naming_another_repo_yields_nothing(self) -> None:
        # The clone's host says nothing about where a DIFFERENT repo lives.
        clear = _ticketless_clear()
        with (
            patch("teatree.core.merge.host_kind.find_project_root", return_value=Path("/tmp/clone")),
            patch(
                "teatree.core.merge.host_kind.remote_url",
                return_value="git@github.com:souliane/teatree.git",
            ),
            pytest.raises(MergePreconditionError, match="could not resolve the forge"),
        ):
            resolve_host_kind(clear, repo_slug=_GITLAB_SLUG)

    def test_unresolvable_forge_fails_loud_instead_of_defaulting_to_github(self) -> None:
        with (
            patch("teatree.core.merge.host_kind.find_project_root", return_value=None),
            pytest.raises(MergePreconditionError, match="--forge <github\\|gitlab>"),
        ):
            resolve_host_kind(_ticketless_clear(), repo_slug=_GITLAB_SLUG)


class TestDeclaredScopeForge(TestCase):
    """``owned_repos`` is forge-host-keyed, so it names where a namespace is hosted."""

    def test_declared_host_resolves_the_namespaces_forge(self) -> None:
        with (
            patch("teatree.core.merge.host_kind.find_project_root", return_value=None),
            patch(
                "teatree.core.merge.host_kind.get_all_overlays",
                return_value={"acme": _overlay_owning({"gitlab.com": ["acme-eng"]})},
            ),
        ):
            assert resolve_host_kind(_ticketless_clear(), repo_slug=_GITLAB_SLUG) == "gitlab"

    def test_a_namespace_no_overlay_declares_stays_unresolved(self) -> None:
        with (
            patch("teatree.core.merge.host_kind.find_project_root", return_value=None),
            patch(
                "teatree.core.merge.host_kind.get_all_overlays",
                return_value={"acme": _overlay_owning({"github.com": ["souliane"]})},
            ),
            pytest.raises(MergePreconditionError, match="could not resolve the forge"),
        ):
            resolve_host_kind(_ticketless_clear(), repo_slug=_GITLAB_SLUG)

    def test_two_forges_claiming_one_namespace_refuse_rather_than_pick(self) -> None:
        with (
            patch("teatree.core.merge.host_kind.find_project_root", return_value=None),
            patch(
                "teatree.core.merge.host_kind.get_all_overlays",
                return_value={
                    "acme": _overlay_owning({"gitlab.com": ["acme-eng"]}),
                    "mirror": _overlay_owning({"github.com": ["acme-eng"]}),
                },
            ),
            pytest.raises(MergePreconditionError, match="ambiguous forge"),
        ):
            resolve_host_kind(_ticketless_clear(), repo_slug=_GITLAB_SLUG)

    def test_a_broken_overlay_registry_degrades_to_the_actionable_refusal(self) -> None:
        with (
            patch("teatree.core.merge.host_kind.find_project_root", return_value=None),
            patch("teatree.core.merge.host_kind.get_all_overlays", side_effect=RuntimeError("registry down")),
            pytest.raises(MergePreconditionError, match="could not resolve the forge"),
        ):
            resolve_host_kind(_ticketless_clear(), repo_slug=_GITLAB_SLUG)


class TestTicketlessGitLabKeystone(TestCase):
    """A managed-repo MR with no teatree Ticket merges through the sanctioned path."""

    def test_ticketless_gitlab_clear_drives_the_glab_transport(self) -> None:
        clear = _ticketless_clear(host_kind="gitlab")
        seed_merge_safe_verdict(slug=_GITLAB_SLUG, pr_id=_MR_IID, sha=_SHA)
        stub = _GlabStub()

        with (
            # The §17.4.3 draft floor probes the live forge, which has no credential
            # here and refuses on UNKNOWN. That floor is pinned in
            # test_authorization_gates.py; this case is about the TRANSPORT.
            patch(_DRAFT_PROBE, return_value=DraftState.NOT_DRAFT),
            patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=stub),
        ):
            outcome = merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        clear.refresh_from_db()
        assert outcome.merged_sha == stub.merge_sha
        assert outcome.ticket_state == ""
        assert clear.consumed_at is not None
        assert MergeAudit.objects.filter(clear=clear).exists()
        assert any("merge_requests" in " ".join(call) for call in stub.calls), (
            f"the GitLab transport was never reached: {stub.calls}"
        )

    def test_ticketless_clear_without_a_forge_refuses_before_touching_a_forge(self) -> None:
        clear = _ticketless_clear()
        with (
            patch("teatree.core.merge.host_kind.find_project_root", return_value=None),
            pytest.raises(MergePreconditionError, match="could not resolve the forge"),
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")


class TestClearRequestCarriesTheForge(TestCase):
    def test_issue_persists_the_requested_forge(self) -> None:
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=_MR_IID,
                slug=_GITLAB_SLUG,
                reviewed_sha=_SHA,
                reviewer_identity="cold-reviewer",
                host_kind="GitLab",
            )
        )
        assert clear.host_kind == "gitlab"

    def test_issue_refuses_an_unknown_forge(self) -> None:
        with pytest.raises(ClearIssuanceError, match="host_kind"):
            MergeClear.issue(
                ClearRequest(
                    pr_id=_MR_IID,
                    slug=_GITLAB_SLUG,
                    reviewed_sha=_SHA,
                    reviewer_identity="cold-reviewer",
                    host_kind="bitbucket",
                )
            )

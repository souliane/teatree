"""An UNREADABLE live head is not a moved head (#4239).

``fetch_live_head_sha`` returns ``""`` for every failure, so an empty result is a
non-answer. The keystone formatted it into a sentence asserting "PR head moved"
and offered two hypotheses that excluded the real one — the merge ran from a
``docker compose exec`` shell carrying none of the ambient forge credentials, so
every forge read returned nothing while the head had not moved at all.

These tests pin the three-state split: resolved+equal proceeds, resolved+differs
keeps the existing head-moved message byte-for-byte, and unreadable gets its own
message that names the credential/venue cause and states the CLEAR survives. The
fail-closed behaviour is unchanged and pinned here too — the unreadable case must
still refuse the merge, leave the CLEAR unconsumed, and issue no merge RPC.

Only the forge subprocess (the network boundary) is stubbed; the candidate
enumeration, reconciliation and keystone preconditions run through real teatree
code. Repo names are neutral placeholders — core/tests stay overlay-agnostic
(BLUEPRINT § 1).
"""

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import PrMergeState
from teatree.core.merge import (
    CodeHostQuery,
    MergePreconditionError,
    assert_merge_preconditions,
    merge_ticket_pr,
    pr_slug_resolution,
)
from teatree.core.merge.head_read_diagnosis import read_credential_env_vars, unreadable_head_advisory
from teatree.core.models import MergeClear, Ticket
from teatree.utils.pr_ref import PrRef

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_REVIEWED_SHA = "a" * 40
_WRONG_SHA = "b" * 40
_PR_ID = 4233

_INITIAL_SLUG = "souliane/teatree"
_CANDIDATE = "downstream-org/downstream-overlay"

_NO_AMBIENT_TOKENS = {"GH_TOKEN": "", "GITHUB_TOKEN": "", "GITLAB_TOKEN": ""}


def _heads(mapping: dict[str, str]) -> Callable[[CodeHostQuery], str]:
    """A ``CodeHostQuery.live_head_sha`` stub reading *mapping* by the bound repo slug."""

    def _live_head(self: CodeHostQuery) -> str:
        return mapping.get(self.ref.slug, "")

    return _live_head


def _unmerged() -> AbstractContextManager[object]:
    """Patch the merge-state read to OPEN — these cases are all unmerged PRs (#4144).

    Stubbed explicitly rather than left to the real transport: the landed-merge
    probe the unreadable-head path now runs would otherwise shell out to ``gh``.
    """
    return patch(
        "teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state",
        autospec=True,
        return_value=PrMergeState(state="OPEN", merge_commit_oid=""),
    )


def _reconcile(*, host_kind: str = "github") -> str:
    return pr_slug_resolution._reconcile_slug_against_reviewed_sha(
        initial_slug=_INITIAL_SLUG,
        pr_id=_PR_ID,
        reviewed_sha=_REVIEWED_SHA,
        host_kind=host_kind,
    )


def _refusal_message(*, mapping: dict[str, str], candidates: list[str], host_kind: str = "github") -> str:
    with (
        patch.dict(os.environ, _NO_AMBIENT_TOKENS, clear=False),
        patch(
            "teatree.core.merge.ci_rollup.CodeHostQuery.live_head_sha",
            autospec=True,
            side_effect=_heads(mapping),
        ),
        _unmerged(),
        patch(
            "teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs",
            return_value=candidates,
        ),
        pytest.raises(MergePreconditionError) as exc,
    ):
        _reconcile(host_kind=host_kind)
    return str(exc.value)


class TestUnreadableHeadIsNotAMovedHead(TestCase):
    """The reported #4239 shape: every forge read empty, head never actually moved."""

    def test_message_does_not_claim_the_head_moved(self) -> None:
        message = _refusal_message(mapping={}, candidates=[_INITIAL_SLUG])

        assert "PR head moved" not in message, (
            f"an unreadable head must not be reported as a moved head; got: {message}"
        )
        assert "could not read the live head" in message

    def test_message_names_the_credential_and_venue_cause(self) -> None:
        message = _refusal_message(mapping={}, candidates=[_INITIAL_SLUG])

        assert "GH_TOKEN" in message
        assert "GITHUB_TOKEN" in message
        assert "docker compose exec" in message

    def test_message_states_the_clear_survives_for_the_authed_loop(self) -> None:
        message = _refusal_message(mapping={}, candidates=[_INITIAL_SLUG])

        assert "CLEAR stays actionable" in message

    def test_message_still_names_every_repo_probed(self) -> None:
        message = _refusal_message(mapping={}, candidates=[_INITIAL_SLUG, _CANDIDATE])

        assert _INITIAL_SLUG in message
        assert _CANDIDATE in message

    def test_gitlab_names_its_own_ambient_credentials(self) -> None:
        message = _refusal_message(mapping={}, candidates=[_INITIAL_SLUG], host_kind="gitlab")

        assert "GITLAB_TOKEN" in message
        assert "GH_TOKEN" not in message


class TestUnreadableHeadStillFailsClosed(TestCase):
    """The behaviour that must NOT regress while the wording is fixed."""

    def test_unreadable_head_refuses_the_merge(self) -> None:
        with (
            patch.dict(os.environ, _NO_AMBIENT_TOKENS, clear=False),
            patch(
                "teatree.core.merge.ci_rollup.CodeHostQuery.live_head_sha",
                autospec=True,
                side_effect=_heads({}),
            ),
            _unmerged(),
            patch(
                "teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs",
                return_value=[_INITIAL_SLUG],
            ),
            pytest.raises(MergePreconditionError),
        ):
            _reconcile()

    def test_keystone_issues_no_merge_and_leaves_the_clear_unconsumed(self) -> None:
        """The message's "the CLEAR stays actionable" claim, verified end to end."""
        clear = MergeClear.objects.create(
            ticket=None,
            pr_id=_PR_ID,
            slug="merge-keystone-unreadable-head",
            reviewed_sha=_REVIEWED_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )
        calls: list[list[str]] = []

        def _gh_answers_nothing(argv: list[str]) -> tuple[int, str, str]:
            calls.append(argv)
            return (1, "", "gh: You are not logged into any GitHub hosts")

        with (
            patch.dict(os.environ, _NO_AMBIENT_TOKENS, clear=False),
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_answers_nothing),
            patch(
                "teatree.core.merge.pr_slug_resolution._project_repo_slug",
                return_value=_INITIAL_SLUG,
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs",
                return_value=[_INITIAL_SLUG],
            ),
            pytest.raises(MergePreconditionError) as exc,
        ):
            merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")

        clear.refresh_from_db()
        assert clear.consumed_at is None, "a refused merge must never consume the CLEAR"
        merge_calls = [argv for argv in calls if "merge" in " ".join(argv) and "pulls" in " ".join(argv)]
        assert merge_calls == [], f"the keystone must issue no merge RPC on an unreadable head; got {merge_calls}"
        assert "PR head moved" not in str(exc.value)


class TestReadableHeadKeepsTheMovedDiagnosis(TestCase):
    """A forge that ANSWERS *for the initial repo* keeps the #1335 head-moved message."""

    def test_initial_repo_answers_a_different_sha(self) -> None:
        message = _refusal_message(
            mapping={_INITIAL_SLUG: _WRONG_SHA},
            candidates=[_INITIAL_SLUG],
        )

        assert "PR head moved" in message
        assert "could not read the live head" not in message

    def test_initial_unreadable_but_a_candidate_answers(self) -> None:
        """The initial head is still a NON-answer, so the drift wording stays off it (#4144).

        The readable candidates change the CAUSE the refusal names — the forge
        plainly works, so the initial repo carries no PR of that number (#1335) —
        never the claim that a head nobody read had moved.
        """
        message = _refusal_message(
            mapping={_CANDIDATE: _WRONG_SHA},
            candidates=[_INITIAL_SLUG, _CANDIDATE],
        )

        assert "PR head moved" not in message
        assert "force-push" not in message
        assert "could not read the live head" in message
        assert "The forge DID answer" in message
        assert "docker compose exec" not in message

    def test_cross_repo_recovery_survives_an_unreadable_initial_read(self) -> None:
        """#1335 recovery is unchanged: an empty initial read still probes candidates."""
        with (
            patch(
                "teatree.core.merge.ci_rollup.CodeHostQuery.live_head_sha",
                autospec=True,
                side_effect=_heads({_CANDIDATE: _REVIEWED_SHA}),
            ),
            patch(
                "teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs",
                return_value=[_INITIAL_SLUG, _CANDIDATE],
            ),
        ):
            resolved = _reconcile()

        assert resolved == _CANDIDATE


class TestStepTwoUnreadableHeadCarriesTheSameAdvisory(TestCase):
    """The sibling unreadable-head site: the §17.4.3 step-2 re-read after reconciliation."""

    def test_unresolved_live_head_names_the_venue_cause(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=_PR_ID,
            slug=_INITIAL_SLUG,
            reviewed_sha=_REVIEWED_SHA,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.DOCS,
        )

        with (
            patch.dict(os.environ, _NO_AMBIENT_TOKENS, clear=False),
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=lambda _argv: (1, "", "not logged in")),
            patch(
                "teatree.core.merge.ci_rollup.CodeHostQuery.pr_changed_paths",
                autospec=True,
                return_value=["README.md"],
            ),
            patch(
                "teatree.core.merge.ci_rollup.CodeHostQuery.live_head_sha",
                autospec=True,
                side_effect=_heads({}),
            ),
            pytest.raises(MergePreconditionError) as exc,
        ):
            assert_merge_preconditions(
                clear=clear,
                executing_loop_identity="merge-loop",
                ref=PrRef(slug=_INITIAL_SLUG, pr_id=_PR_ID, host_kind="github"),
            )

        message = str(exc.value)
        assert "could not resolve the live head SHA" in message
        assert "docker compose exec" in message
        assert "CLEAR stays actionable" in message
        assert "PR head moved" not in message


class TestAdvisoryNeverOverClaims(TestCase):
    """A venue that DOES carry a credential gets the other branch."""

    def test_unknown_host_kind_falls_back_to_the_github_credentials(self) -> None:
        assert read_credential_env_vars("bitbucket") == read_credential_env_vars("github")

    def test_present_token_rules_the_credential_out(self) -> None:
        with patch.dict(os.environ, {**_NO_AMBIENT_TOKENS, "GH_TOKEN": "x" * 8}, clear=False):
            advisory = unreadable_head_advisory("github")

        assert "GH_TOKEN" in advisory
        assert "not the cause" in advisory
        assert "docker compose exec" not in advisory

    def test_absent_token_names_the_venue_cause(self) -> None:
        with patch.dict(os.environ, _NO_AMBIENT_TOKENS, clear=False):
            advisory = unreadable_head_advisory("github")

        assert "docker compose exec" in advisory
        assert "not the cause" not in advisory


class TestAdvisoryNamesTheChainTheFailingReadActuallyUSES(TestCase):
    """#4239's own defect class, applied to the diagnosis: never name a chain the read skips."""

    def test_gitlab_never_names_a_variable_no_read_path_consults(self) -> None:
        """``glab`` was retired from the merge transport (#4007) — ``GLAB_TOKEN`` is fiction."""
        assert "GLAB_TOKEN" not in read_credential_env_vars("gitlab")

        with patch.dict(os.environ, _NO_AMBIENT_TOKENS, clear=False):
            assert "GLAB_TOKEN" not in unreadable_head_advisory("gitlab")

    def test_gitlab_names_the_pass_fallback_the_absent_env_var_did_not_rule_out(self) -> None:
        """GitLab resolves ``GITLAB_TOKEN`` env-FIRST then ``pass``, so an unset var proves nothing."""
        with patch.dict(os.environ, _NO_AMBIENT_TOKENS, clear=False):
            advisory = unreadable_head_advisory("gitlab")

        assert "gitlab/pat" in advisory

    def test_github_names_the_gh_config_fallback(self) -> None:
        """``gh`` also authenticates from its own config file, so an unset var proves nothing."""
        with patch.dict(os.environ, _NO_AMBIENT_TOKENS, clear=False):
            advisory = unreadable_head_advisory("github")

        assert "gh" in advisory
        assert "config" in advisory


class TestProbeReportsEveryCandidateHead(TestCase):
    """The probe returns per-candidate heads — what lets unreadable be told from wrong."""

    def test_unreadable_candidate_is_reported_as_empty(self) -> None:
        with patch(
            "teatree.core.merge.ci_rollup.CodeHostQuery.live_head_sha",
            autospec=True,
            side_effect=_heads({_CANDIDATE: _WRONG_SHA}),
        ):
            heads = pr_slug_resolution._probe_candidate_heads(
                query=CodeHostQuery.for_ref(PrRef(slug=_INITIAL_SLUG, pr_id=_PR_ID)),
                candidates=[_CANDIDATE, "other-org/other-repo"],
            )

        assert heads == {_CANDIDATE: _WRONG_SHA, "other-org/other-repo": ""}

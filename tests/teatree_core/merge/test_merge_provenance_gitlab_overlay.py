"""GitLab overlay MRs cross the same merge-provenance gate (#3244 / #3313).

The overlay fail-closed proof: a GitLab MR whose ``source_project_id`` differs
from its ``target_project_id`` is a fork, so ``CodeHostQuery.pr_same_repo``
resolves to ``False`` through the ``glab`` transport and the keystone
``assert_merge_provenance_trusted`` refuses it — the same gate GitHub PRs cross.
And per the #3313 hardening a same-project-id (non-fork) MR is NOT trusted
unconditionally either: it is conjoined with the identity+visibility check, so an
untrusted author on a PUBLIC repo still holds for a human while an internal repo
trusts any author — identical to the GitHub same-repo contract.
Only the ``glab`` subprocess is stubbed; the CodeHostQuery resolution + gate are real.
"""

import json
from collections.abc import Callable
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge.authorization import assert_merge_provenance_trusted
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.review import author_trust

pytestmark = pytest.mark.django_db  # ast-grep-ignore: ac-django-no-pytest-django-db

_SLUG = "acme/widget"
_IID = 6264

_Runner = Callable[[list[str]], tuple[int, str, str]]


def _glab_runner_returning(mr: dict[str, object]) -> _Runner:
    def _run(_argv: list[str]) -> tuple[int, str, str]:
        return (0, json.dumps(mr), "")

    return _run


class TestGitLabOverlayProvenanceGate(TestCase):
    def test_fork_mr_distinct_project_ids_refused(self) -> None:
        runner = _glab_runner_returning(
            {"source_project_id": 9, "target_project_id": 7, "author": {"username": "souliane"}},
        )
        with (
            patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=runner),
            pytest.raises(MergePreconditionError, match="fork / cross-repo"),
        ):
            assert_merge_provenance_trusted(slug=_SLUG, pr_id=_IID, host_kind="gitlab")

    def test_same_project_id_untrusted_author_holds_on_public_repo(self) -> None:
        # #3313 hardening: a same-project-id (non-fork) MR is NOT trusted
        # unconditionally on a PUBLIC repo — a push-access account not in the trust
        # set (an added collaborator, a compromised token) still holds for a human.
        runner = _glab_runner_returning(
            {"source_project_id": 7, "target_project_id": 7, "author": {"username": "any-bot"}},
        )
        with (
            patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=runner),
            patch.object(author_trust, "repo_is_internal", return_value=False),
            pytest.raises(MergePreconditionError, match="untrusted author on a public repo"),
        ):
            assert_merge_provenance_trusted(slug=_SLUG, pr_id=_IID, host_kind="gitlab")

    def test_same_project_id_on_internal_repo_passes(self) -> None:
        # On a private/internal overlay repo the operator owns access control, so a
        # same-project-id MR from any author is trusted (the internal-repo branch).
        runner = _glab_runner_returning(
            {"source_project_id": 7, "target_project_id": 7, "author": {"username": "any-bot"}},
        )
        with (
            patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=runner),
            patch.object(author_trust, "repo_is_internal", return_value=True),
        ):
            assert_merge_provenance_trusted(slug=_SLUG, pr_id=_IID, host_kind="gitlab")

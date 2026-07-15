"""GitLab overlay MRs cross the same merge-provenance gate (#3244).

The overlay fail-closed proof: a GitLab MR whose ``source_project_id`` differs
from its ``target_project_id`` is a fork, so ``CodeHostQuery.pr_same_repo``
resolves to ``False`` through the ``glab`` transport and the keystone
``assert_merge_provenance_trusted`` refuses it — the same gate GitHub PRs cross.
Only the ``glab`` subprocess is stubbed; the CodeHostQuery resolution + gate are real.
"""

import json
from collections.abc import Callable
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge.authorization import assert_merge_provenance_trusted
from teatree.core.merge.errors import MergePreconditionError

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

    def test_same_project_id_mr_passes(self) -> None:
        runner = _glab_runner_returning(
            {"source_project_id": 7, "target_project_id": 7, "author": {"username": "any-bot"}},
        )
        with patch("teatree.backends.forge_merge_rpc.glab_runner", return_value=runner):
            assert_merge_provenance_trusted(slug=_SLUG, pr_id=_IID, host_kind="gitlab")

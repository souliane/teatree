"""Path-based substrate classifier — label-independent substrate detection.

``blast_class`` is the orchestrator's (or a human's) judgment and defaults to
``logic``. Under ``autonomy = full`` a substrate diff mislabeled ``logic`` would
otherwise auto-merge silently. The path classifier makes the substrate guarantee
reliable: a diff touching a substrate path (``BLUEPRINT.md``, governance docs,
anything under ``src/teatree/core/merge/``) is substrate regardless of the label.

Pure-logic unit tests for :func:`diff_paths_are_substrate`; the model-method
integration (``is_substrate()`` consulting ``touched_paths``) is a thin DB test.
"""

import pytest
from django.test import TestCase

from teatree.core.models import MergeClear
from teatree.core.models.merge_clear import diff_paths_are_substrate

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestDiffPathsAreSubstrate:
    """The pure path classifier — no DB, no model instance."""

    def test_blueprint_root_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["BLUEPRINT.md"]) is True

    def test_blueprint_appendix_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["docs/blueprint/17-merge.md"]) is True

    def test_core_merge_dir_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["src/teatree/core/merge/authorization.py"]) is True

    def test_nested_under_core_merge_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["src/teatree/core/merge/sub/deep.py"]) is True

    def test_governance_claude_md_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["CLAUDE.md"]) is True

    def test_governance_agents_md_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["AGENTS.md"]) is True

    def test_nested_governance_doc_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["src/teatree/core/CLAUDE.md"]) is True

    def test_ordinary_logic_path_is_not_substrate(self) -> None:
        assert diff_paths_are_substrate(["src/teatree/loop/scanners/pr_sweep.py"]) is False

    def test_ordinary_doc_path_is_not_substrate(self) -> None:
        assert diff_paths_are_substrate(["README.md", "docs/usage.md"]) is False

    def test_empty_paths_is_not_substrate(self) -> None:
        assert diff_paths_are_substrate([]) is False

    def test_mixed_diff_with_one_substrate_path_is_substrate(self) -> None:
        # A single substrate path in an otherwise-logic diff still classifies
        # the whole change as substrate — the guarantee is "touches substrate".
        assert diff_paths_are_substrate(["src/teatree/cli/x.py", "src/teatree/core/merge/x.py"]) is True

    def test_leading_slash_and_dotslash_are_normalized(self) -> None:
        assert diff_paths_are_substrate(["./src/teatree/core/merge/x.py"]) is True
        assert diff_paths_are_substrate(["/BLUEPRINT.md"]) is True

    def test_substring_lookalike_is_not_substrate(self) -> None:
        # ``BLUEPRINT.md.bak`` and a sibling dir that merely starts with the
        # same prefix must not be misclassified — match on path components.
        assert diff_paths_are_substrate(["docs/BLUEPRINT.md.bak"]) is False
        assert diff_paths_are_substrate(["src/teatree/core/merger/x.py"]) is False


class TestIsSubstrateConsultsTouchedPaths(TestCase):
    """``MergeClear.is_substrate()`` returns true on a substrate diff regardless of the label."""

    def _logic_clear(self) -> MergeClear:
        return MergeClear.objects.create(
            pr_id=4242,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
        )

    def test_logic_clear_with_substrate_path_is_substrate(self) -> None:
        # The anti-vacuity test (c): blast_class left at the 'logic' default,
        # but the diff touches src/teatree/core/merge/ -> is_substrate() True.
        clear = self._logic_clear()
        assert clear.is_substrate() is False  # no paths attached yet
        clear.touched_paths = ("src/teatree/core/merge/authorization.py",)
        assert clear.is_substrate() is True

    def test_logic_clear_with_only_logic_paths_is_not_substrate(self) -> None:
        clear = self._logic_clear()
        clear.touched_paths = ("src/teatree/loop/scanners/pr_sweep.py",)
        assert clear.is_substrate() is False

    def test_substrate_label_is_substrate_even_with_logic_paths(self) -> None:
        clear = MergeClear.objects.create(
            pr_id=4243,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
        )
        clear.touched_paths = ("src/teatree/loop/scanners/pr_sweep.py",)
        assert clear.is_substrate() is True

    def test_touched_paths_defaults_empty(self) -> None:
        clear = self._logic_clear()
        assert clear.touched_paths == ()

"""Path-based substrate classifier — label-independent substrate detection.

``blast_class`` is the orchestrator's (or a human's) judgment and defaults to
``logic``. Under ``autonomy = full`` a substrate diff mislabeled ``logic`` would
otherwise auto-merge silently. The path classifier makes the substrate guarantee
reliable: a diff touching a substrate path (``BLUEPRINT.md``, governance docs,
anything under ``src/teatree/core/merge/``) is substrate regardless of the label.

Pure-logic unit tests for :func:`diff_paths_are_substrate`; the model-method
integration (``is_substrate()`` consulting ``touched_paths``) is a thin DB test.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import MergeClear, merge_clear
from teatree.core.models.merge_clear import _SUBSTRATE_PATH_PREFIXES, diff_paths_are_substrate

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

    def test_trust_seam_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["src/teatree/core/review/author_trust.py"]) is True

    def test_intake_decision_seam_is_substrate(self) -> None:
        # The ONE decision function that answers "who may the factory work for".
        assert diff_paths_are_substrate(["src/teatree/core/intake/factory_admission.py"]) is True

    def test_intake_scanner_seam_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["src/teatree/loop/scanners/issue_intake.py"]) is True

    def test_stranger_pr_admission_gate_is_substrate(self) -> None:
        # The PR-side half of the same trust boundary: a diff that admits an
        # untrusted author's PR to the reviewer must not auto-merge as logic.
        assert diff_paths_are_substrate(["src/teatree/core/review/stranger_pr.py"]) is True

    def test_intake_wiring_seam_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["src/teatree/loop/scanner_factories.py"]) is True

    def test_every_intake_seam_is_named_in_the_prefix_list(self) -> None:
        """Anti-vacuity: each seam is substrate BECAUSE the prefix list names it.

        Dropping any one entry turns its assertion above RED — the guard cannot
        pass on a path that is no longer classified.
        """
        seams = (
            "src/teatree/core/intake/factory_admission.py",
            "src/teatree/loop/scanners/issue_intake.py",
            "src/teatree/core/review/stranger_pr.py",
            "src/teatree/loop/scanner_factories.py",
        )
        assert set(seams) <= set(_SUBSTRATE_PATH_PREFIXES)

    def test_a_seam_dropped_from_the_prefix_list_is_no_longer_substrate(self) -> None:
        """The classifier has no fallback that would keep a dropped seam substrate."""
        without_intake = tuple(
            p for p in _SUBSTRATE_PATH_PREFIXES if p != "src/teatree/core/intake/factory_admission.py"
        )
        with patch.object(merge_clear, "_SUBSTRATE_PATH_PREFIXES", without_intake):
            assert diff_paths_are_substrate(["src/teatree/core/intake/factory_admission.py"]) is False

    def test_the_retired_intake_scanner_path_is_not_classified(self) -> None:
        """The deleted module must not linger in the prefix list as a dead assertion."""
        assert diff_paths_are_substrate(["src/teatree/loop/scanners/issue_implementer.py"]) is False

    def test_merge_classifier_module_is_substrate(self) -> None:
        # The module that DEFINES the trust boundary must class itself substrate:
        # a PR loosening this very classifier can no longer auto-merge as logic.
        assert diff_paths_are_substrate(["src/teatree/core/models/merge_clear.py"]) is True

    def test_review_verdict_record_is_substrate(self) -> None:
        # The cold-review record + maker!=checker guard is a trust seam.
        assert diff_paths_are_substrate(["src/teatree/core/models/review_verdict.py"]) is True

    def test_merge_safety_gate_is_substrate(self) -> None:
        # Every gate under core/gates/ enforces a merge/safety invariant.
        assert diff_paths_are_substrate(["src/teatree/core/gates/merge_guard.py"]) is True

    def test_schema_migration_is_substrate(self) -> None:
        # A schema change (incl. destructive DROP / data-rewrite) mutates the
        # durable governance store -> substrate, never a silent logic auto-merge
        # (this gap is why #3464's migration auto-merged).
        assert diff_paths_are_substrate(["src/teatree/core/migrations/0001_initial.py"]) is True

    def test_autonomy_config_is_substrate(self) -> None:
        # config/ holds the autonomy tiers and the substrate_auto_merge_authorized_by
        # default -> editing it changes how the factory governs itself.
        assert diff_paths_are_substrate(["src/teatree/config/settings.py"]) is True

    def test_on_behalf_gate_is_substrate(self) -> None:
        assert diff_paths_are_substrate(["src/teatree/on_behalf_gate.py"]) is True

    def test_ordinary_cli_check_is_not_substrate(self) -> None:
        # A clearly-logic path (a doctor check) stays logic after the widening.
        assert diff_paths_are_substrate(["src/teatree/cli/doctor/checks.py"]) is False

    def test_ordinary_test_file_is_not_substrate(self) -> None:
        assert diff_paths_are_substrate(["tests/teatree_core/models/test_merge_clear_substrate_paths.py"]) is False

    def test_models_sibling_is_not_substrate(self) -> None:
        # The widening pins merge_clear.py / review_verdict.py exactly — an
        # unrelated model in core/models/ must NOT become substrate.
        assert diff_paths_are_substrate(["src/teatree/core/models/ticket.py"]) is False

    def test_config_lookalike_is_not_substrate(self) -> None:
        # config/ has a trailing slash -> a sibling like configuration.py or the
        # cli/config.py helper is not the autonomy-config package.
        assert diff_paths_are_substrate(["src/teatree/cli/config.py"]) is False
        assert diff_paths_are_substrate(["src/teatree/core/migrationsx/x.py"]) is False

    def test_safety_hooks_are_substrate(self) -> None:
        assert diff_paths_are_substrate(["hooks/scripts/question_gates.py"]) is True

    def test_trust_seam_lookalike_is_not_substrate(self) -> None:
        # ``author_trust.py`` is pinned exactly — a sibling helper is not the seam.
        assert diff_paths_are_substrate(["src/teatree/core/review/author_trust_helpers.py"]) is False
        assert diff_paths_are_substrate(["hooksmith/x.py"]) is False

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

    def test_self_governance_seam_holds_even_with_logic_label(self) -> None:
        # #3244: an autonomous PR that touches the trust seam is substrate
        # regardless of the 'logic' default — the factory cannot loosen its own
        # guardrails unattended; the CLEAR ping-and-holds for the owner.
        clear = self._logic_clear()
        clear.touched_paths = ("src/teatree/core/review/author_trust.py",)
        assert clear.is_substrate() is True

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

    def test_indeterminate_paths_fail_closed_to_substrate(self) -> None:
        # A logic-labelled CLEAR whose changed-path list could not be read to
        # completion (truncated/paginated/errored) can no longer be PROVEN
        # non-substrate — it holds as substrate rather than silently auto-merging.
        clear = self._logic_clear()
        assert clear.is_substrate() is False
        clear.substrate_paths_indeterminate = True
        assert clear.is_substrate() is True

    def test_substrate_paths_indeterminate_defaults_false(self) -> None:
        clear = self._logic_clear()
        assert clear.substrate_paths_indeterminate is False

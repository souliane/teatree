"""The regression-corpus predicate bodies, exercised in isolation.

Mirrors ``src/teatree/eval/regression_corpus_predicates.py``. Each ``_check_*``
calls the REAL gate/checker code on a must-block and a must-allow input and
returns ``True`` only when both directions hold; ``run_regression_corpus`` wires
them into its check table (covered in ``tests/eval_replay/test_regression_corpus.py``).
Here each predicate is called directly so a regression in the predicate logic
itself (not just the corpus orchestration) is observable, plus an anti-vacuity
proof: breaking the underlying real function flips a predicate to ``False``.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.config.resolution import get_effective_settings
from teatree.config.settings import Autonomy
from teatree.core.models import ConfigSetting
from teatree.core.overlay_loader import infer_overlay_for_url
from teatree.eval import regression_corpus_predicates as predicates


class TestNonDbPredicatesHoldOnRealCode(TestCase):
    """The predicates that need no ORM call the real code path and return True."""

    def test_branch_currency_conflict_only(self) -> None:
        assert predicates._check_branch_currency_conflict_only() is True

    def test_account_switch_detect_and_recover(self) -> None:
        assert predicates._check_account_switch_detect_and_recover() is True

    def test_private_repo_allowlist_path_segment_match(self) -> None:
        assert predicates._check_private_repo_allowlist_path_segment_match() is True

    def test_banned_terms_scanner_fails_closed_on_crash(self) -> None:
        assert predicates._check_banned_terms_scanner_fails_closed_on_crash() is True

    def test_forge_resolves_by_host_not_token(self) -> None:
        assert predicates._check_forge_resolves_by_host_not_token() is True

    def test_mr_description_first_line_validated(self) -> None:
        assert predicates._check_mr_description_first_line_validated() is True


class TestDbBackedPredicatesHoldOnRealCode(TestCase):
    """The ORM-backed predicates hold against the migrated test DB."""

    def test_substrate_human_authorize_floor(self) -> None:
        assert predicates._check_merge_precondition_substrate_human_authorize() is True

    def test_substrate_full_autonomy_holds(self) -> None:
        assert predicates._check_merge_precondition_substrate_full_autonomy_holds() is True

    def test_maker_is_not_checker(self) -> None:
        assert predicates._check_merge_precondition_maker_is_not_checker() is True

    def test_loop_owner_lease_pid_anchored(self) -> None:
        assert predicates._check_loop_owner_lease_pid_anchored() is True

    def test_ship_branch_reconcile_renamed(self) -> None:
        assert predicates._check_ship_branch_reconcile_renamed() is True


class TestPredicatesAreAntiVacuous(TestCase):
    """Breaking the underlying real function flips the predicate to False.

    A predicate that returned True regardless of the code path would guard
    nothing; these prove each direction is actually consulted.
    """

    def test_forge_predicate_red_when_host_classifier_lies(self) -> None:
        with patch("teatree.utils.forge.forge_from_remote", return_value="github"):
            # gitlab/unknown remotes now also resolve to "github" → must-allow legs fail.
            assert predicates._check_forge_resolves_by_host_not_token() is False

    def test_first_line_predicate_red_when_validator_accepts_everything(self) -> None:
        with patch("teatree.core.review.mr_metadata.validate_mr_metadata", return_value=[]):
            # A validator that never rejects → the must-reject leg fails.
            assert predicates._check_mr_description_first_line_validated() is False

    def test_allowlist_predicate_red_when_matcher_substring_matches(self) -> None:
        with patch("teatree.hooks._repo_visibility.slug_is_allowlisted_private", return_value=True):
            # A matcher that flags the public alias-glued slug → must-not-match leg fails.
            assert predicates._check_private_repo_allowlist_path_segment_match() is False


class TestStagedAutonomyHermeticAgainstDbTier(TestCase):
    """The substrate-floor helper pins autonomy regardless of a live DB override (#1775 host leak).

    ``autonomy`` is DB-home under the #1775 partition: it resolves solely from
    the ``ConfigSetting`` store, so a host carrying a per-overlay ``autonomy``
    row would silently override whatever the floor check stages. The helper
    stages autonomy through that same DB-home seam (the per-overlay row reader)
    and neutralises the global DB scope and the env tier, so the staged value is
    authoritative regardless of any live host row. Both tests go RED on a helper
    that fails to neutralise the loaders (the seeded ``full`` row leaks past the
    staged ``babysit``).
    """

    def _pin_overlay_autonomy_full_in_db(self) -> str:
        overlay_name = infer_overlay_for_url("souliane/teatree") or "t3-teatree"
        ConfigSetting.objects.set_value("autonomy", Autonomy.FULL.value, scope=overlay_name)
        return overlay_name

    def test_staged_babysit_wins_over_db_full_override(self) -> None:
        overlay_name = self._pin_overlay_autonomy_full_in_db()
        with predicates._staged_overlay_autonomy(overlay_name, "babysit"):
            resolved = get_effective_settings(overlay_name).autonomy
        assert resolved is Autonomy.BABYSIT

    def test_substrate_floor_holds_with_live_full_db_override(self) -> None:
        self._pin_overlay_autonomy_full_in_db()
        # The below-full floor must still BLOCK a human-less substrate CLEAR even
        # though the host DB pins this overlay to full.
        assert predicates._check_merge_precondition_substrate_human_authorize() is True

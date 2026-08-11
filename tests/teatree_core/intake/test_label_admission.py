"""The shared label gate every issue-intake path runs before creating work."""

from teatree.core.intake.label_admission import LabelPolicy, excluded, intake_admits

READY = ("ready-for-dev",)
EXCLUDE = ("blocked",)


class TestExcludedPredicate:
    """The one definition of "excluded" both intakes share (#4134)."""

    def test_a_matching_label_is_excluded(self) -> None:
        assert excluded(["backend", "blocked"], EXCLUDE) is True

    def test_an_unmatched_label_is_not_excluded(self) -> None:
        assert excluded(["backend"], EXCLUDE) is False

    def test_an_empty_denylist_excludes_nothing(self) -> None:
        assert excluded(["blocked"], ()) is False


class TestEmptyAllowlistAdmitsEverything:
    def test_no_policy_at_all_admits(self) -> None:
        assert intake_admits(["anything"], (), ()) is True

    def test_no_labels_and_no_policy_admits(self) -> None:
        assert intake_admits([], (), ()) is True

    def test_empty_ready_labels_admits_an_unlabelled_issue(self) -> None:
        assert intake_admits([], (), EXCLUDE) is True


class TestAllowlist:
    def test_admits_when_a_ready_label_is_present(self) -> None:
        assert intake_admits(["ready-for-dev", "backend"], READY, ()) is True

    def test_refuses_when_no_ready_label_is_present(self) -> None:
        assert intake_admits(["backend"], READY, ()) is False

    def test_refuses_an_unlabelled_issue(self) -> None:
        assert intake_admits([], READY, ()) is False


class TestDenylist:
    def test_refuses_when_an_exclude_label_is_present(self) -> None:
        assert intake_admits(["ready-for-dev", "blocked"], READY, EXCLUDE) is False

    def test_denylist_applies_without_an_allowlist(self) -> None:
        assert intake_admits(["blocked"], (), EXCLUDE) is False

    def test_admits_when_ready_and_not_excluded(self) -> None:
        assert intake_admits(["ready-for-dev"], READY, EXCLUDE) is True


class TestLabelPolicyCarriesTheSameDecision:
    def test_default_policy_admits_everything(self) -> None:
        assert LabelPolicy().admits(["anything"]) is True

    def test_policy_delegates_to_the_shared_predicate(self) -> None:
        policy = LabelPolicy(ready_labels=READY, exclude_labels=EXCLUDE)

        assert policy.admits(["ready-for-dev"]) is True
        assert policy.admits(["backend"]) is False
        assert policy.admits(["ready-for-dev", "blocked"]) is False

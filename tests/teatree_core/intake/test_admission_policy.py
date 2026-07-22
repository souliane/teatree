"""The single per-overlay admission verdict for issue intake (#3573).

Covers the three policy cases (``all`` / ``assigned`` / ``assigned_and_labeled``),
the HARD INVARIANT floor (never auto-work an unassigned AND unlabeled issue under
any non-``all`` policy), the forge-shape assignee/label extraction, and the
config resolution — including the ``t3-teatree`` overlay code default of ``all``.
"""

import pytest
from django.test import TestCase

from teatree.config.enums import AdmissionPolicy
from teatree.core.intake.admission_policy import (
    AUTO_LABEL,
    admit_issue,
    admits,
    issue_assignees,
    issue_labels,
    resolve_admission_policy,
)
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.overlay import OverlayConfig
from teatree.types import RawAPIDict

OWNER = "alice"
_NON_ALL = [AdmissionPolicy.ASSIGNED, AdmissionPolicy.ASSIGNED_AND_LABELED]


class TestAdmitsVerdict:
    """The pure verdict — the three cases plus the floor, no DB."""

    def test_all_admits_unassigned_and_unlabeled(self) -> None:
        assert admits(AdmissionPolicy.ALL, assigned=False, labeled=False) is True

    def test_all_admits_everything(self) -> None:
        for assigned in (True, False):
            for labeled in (True, False):
                assert admits(AdmissionPolicy.ALL, assigned=assigned, labeled=labeled) is True

    def test_assigned_admits_assigned_only(self) -> None:
        assert admits(AdmissionPolicy.ASSIGNED, assigned=True, labeled=False) is True

    def test_assigned_rejects_unassigned_even_when_labeled(self) -> None:
        assert admits(AdmissionPolicy.ASSIGNED, assigned=False, labeled=True) is False

    def test_assigned_and_labeled_requires_both(self) -> None:
        assert admits(AdmissionPolicy.ASSIGNED_AND_LABELED, assigned=True, labeled=True) is True
        assert admits(AdmissionPolicy.ASSIGNED_AND_LABELED, assigned=True, labeled=False) is False
        assert admits(AdmissionPolicy.ASSIGNED_AND_LABELED, assigned=False, labeled=True) is False

    @pytest.mark.parametrize("policy", _NON_ALL)
    def test_floor_rejects_unassigned_and_unlabeled_under_every_non_all_policy(self, policy: AdmissionPolicy) -> None:
        assert admits(policy, assigned=False, labeled=False) is False


class TestIssueExtraction:
    """Assignee + label extraction across the GitHub / GitLab payload shapes."""

    def test_github_shaped_assignees_list(self) -> None:
        issue: RawAPIDict = {"assignees": [{"login": "Alice"}, {"login": "bob"}]}
        assert issue_assignees(issue) == frozenset({"alice", "bob"})

    def test_gitlab_shaped_assignees_and_singular_assignee(self) -> None:
        issue: RawAPIDict = {"assignees": [{"username": "carol"}], "assignee": {"username": "dave"}}
        assert issue_assignees(issue) == frozenset({"carol", "dave"})

    def test_no_assignees_is_empty(self) -> None:
        assert issue_assignees({"title": "x"}) == frozenset()

    def test_labels_across_string_and_object_shapes(self) -> None:
        issue: RawAPIDict = {"labels": ["t3-auto", {"name": "bug"}]}
        assert issue_labels(issue) == frozenset({"t3-auto", "bug"})
        assert AUTO_LABEL in issue_labels(issue)


class TestResolveAdmissionPolicy(TestCase):
    """The effective policy per overlay — default, DB override, and code default."""

    def test_default_is_the_strictest_colleague_safe_policy(self) -> None:
        # An unconfigured / unregistered overlay resolves to the shipped default.
        assert resolve_admission_policy("colleague-overlay") is AdmissionPolicy.ASSIGNED_AND_LABELED

    def test_db_row_overrides_the_default(self) -> None:
        ConfigSetting.objects.set_value("admission_policy", "assigned")
        assert resolve_admission_policy("colleague-overlay") is AdmissionPolicy.ASSIGNED

    def test_teatree_overlay_code_default_is_all(self) -> None:
        assert resolve_admission_policy("t3-teatree") is AdmissionPolicy.ALL

    def test_overlay_config_mirrors_dataclass_default(self) -> None:
        # A bare overlay config is a no-op: it mirrors the strict dataclass default.
        assert OverlayConfig().admission_policy is AdmissionPolicy.ASSIGNED_AND_LABELED

    def test_teatree_overlay_settings_load_all(self) -> None:
        config = OverlayConfig(settings_module="teatree.contrib.t3_teatree.overlay_settings")
        assert config.admission_policy is AdmissionPolicy.ALL


class TestAdmitIssue(TestCase):
    """The config-resolving SSOT both scanners consume."""

    def _issue(self, *, assignee: str = "", label: str = "") -> RawAPIDict:
        issue: RawAPIDict = {"web_url": "https://example.com/issues/1", "title": "do it"}
        if assignee:
            issue["assignees"] = [{"login": assignee}]
        if label:
            issue["labels"] = [label]
        return issue

    def test_all_policy_admits_unassigned_unlabeled(self) -> None:
        ConfigSetting.objects.set_value("admission_policy", "all")
        assert admit_issue(self._issue(), overlay="ov", owner_handles=[OWNER]) is True

    def test_assigned_policy_admits_owner_assigned(self) -> None:
        ConfigSetting.objects.set_value("admission_policy", "assigned")
        assert admit_issue(self._issue(assignee=OWNER), overlay="ov", owner_handles=[OWNER]) is True

    def test_assigned_policy_rejects_unassigned(self) -> None:
        ConfigSetting.objects.set_value("admission_policy", "assigned")
        assert admit_issue(self._issue(), overlay="ov", owner_handles=[OWNER]) is False

    def test_assigned_and_labeled_requires_both(self) -> None:
        ConfigSetting.objects.set_value("admission_policy", "assigned_and_labeled")
        both = self._issue(assignee=OWNER, label=AUTO_LABEL)
        assert admit_issue(both, overlay="ov", owner_handles=[OWNER]) is True
        assigned_only = self._issue(assignee=OWNER)
        assert admit_issue(assigned_only, overlay="ov", owner_handles=[OWNER]) is False

    def test_default_rejects_colleague_unassigned_unlabeled(self) -> None:
        # No config row: the strict default. A colleague's issue not assigned to
        # the owner and without the t3-auto label is refused.
        assert admit_issue(self._issue(assignee="colleague"), overlay="ov", owner_handles=[OWNER]) is False

    def test_owner_alias_match_is_case_insensitive(self) -> None:
        ConfigSetting.objects.set_value("admission_policy", "assigned")
        issue = self._issue(assignee="Alice")
        assert admit_issue(issue, overlay="ov", owner_handles=["alice"]) is True

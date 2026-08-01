"""Which branch a shipped PR targets, and which ref the currency gates predict against.

The ``target_branch`` setting (#940) exists so a whole line of work stacks onto
ONE long-lived integration branch. A resolver with no producer is a setting that
silently does nothing, so these tests pin both halves: the bare name the forge
API needs, and the remote-qualified ref the merge-prediction gates need.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import ConfigSetting, Ticket
from teatree.core.worktree.target_branch import (
    bare_target,
    qualified_target,
    resolve_pr_target_branch,
    resolve_target_branch,
)

_INTEGRATION = "chore/long-lived-integration"
_DEFAULT_BRANCH = "teatree.core.worktree.target_branch.git.default_branch"


class TestQualification(TestCase):
    def test_a_prefixed_branch_name_is_not_remote_qualified(self) -> None:
        # Slashes are normal in branch names; only a leading remote makes a ref remote.
        assert qualified_target("release/1.2") == "origin/release/1.2"

    def test_an_already_remote_ref_is_left_alone(self) -> None:
        assert qualified_target("origin/main") == "origin/main"
        assert qualified_target("upstream/main") == "upstream/main"

    def test_bare_strips_only_the_remote_prefix(self) -> None:
        assert bare_target("origin/release/1.2") == "release/1.2"
        assert bare_target("  release/1.2  ") == "release/1.2"


class TestResolvePrTargetBranch(TestCase):
    """The bare branch a ``PullRequestSpec`` targets — ``""`` defers to the forge default."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="t3-teatree", extra={})

    def test_unset_setting_defers_to_the_forge_default(self) -> None:
        assert resolve_pr_target_branch(self.ticket, branch="feat/x") == ""

    def test_configured_setting_is_the_pr_target(self) -> None:
        ConfigSetting.objects.set_value("target_branch", _INTEGRATION)
        assert resolve_pr_target_branch(self.ticket, branch="feat/x") == _INTEGRATION

    def test_ticket_override_outranks_the_setting(self) -> None:
        ConfigSetting.objects.set_value("target_branch", _INTEGRATION)
        self.ticket.extra = {"target_branch": "release/1.2"}
        assert resolve_pr_target_branch(self.ticket, branch="feat/x") == "release/1.2"

    def test_the_integration_branch_does_not_target_itself(self) -> None:
        ConfigSetting.objects.set_value("target_branch", _INTEGRATION)
        assert resolve_pr_target_branch(self.ticket, branch=_INTEGRATION) == ""

    def test_an_origin_qualified_setting_is_returned_bare(self) -> None:
        # The forge API takes a branch name; ``origin/`` is a local ref prefix
        # that GitLab's ``target_branch`` and ``gh pr create --base`` both reject.
        ConfigSetting.objects.set_value("target_branch", f"origin/{_INTEGRATION}")
        assert resolve_pr_target_branch(self.ticket, branch="feat/x") == _INTEGRATION

    def test_an_orphan_branch_with_no_ticket_still_honours_the_setting(self) -> None:
        # ``pr ensure-pr`` opens PRs for branches with no owning Ticket row.
        ConfigSetting.objects.set_value("target_branch", _INTEGRATION)
        assert resolve_pr_target_branch(None, branch="feat/x") == _INTEGRATION


class TestResolveTargetBranch(TestCase):
    """The remote-qualified ref the merge-prediction gates fetch and compare against."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="t3-teatree", extra={})

    def test_unset_setting_keeps_the_repo_default(self) -> None:
        with patch(_DEFAULT_BRANCH, return_value="main"):
            assert resolve_target_branch(self.ticket, "/repo", branch="feat/x") == "origin/main"

    def test_configured_setting_is_used_when_the_ticket_names_nothing(self) -> None:
        ConfigSetting.objects.set_value("target_branch", _INTEGRATION)
        with patch(_DEFAULT_BRANCH, return_value="main"):
            assert resolve_target_branch(self.ticket, "/repo", branch="feat/x") == f"origin/{_INTEGRATION}"

    def test_a_prefixed_ticket_override_is_remote_qualified(self) -> None:
        # ``release/1.2`` returned bare would make ``git fetch release`` the
        # fetch step — it fails, and the gate then fails OPEN with no probe.
        self.ticket.extra = {"target_branch": "release/1.2"}
        with patch(_DEFAULT_BRANCH, return_value="main"):
            assert resolve_target_branch(self.ticket, "/repo", branch="feat/x") == "origin/release/1.2"

    def test_the_integration_branch_does_not_target_itself(self) -> None:
        # A PR targeting its own branch is a no-op, and the currency gate would
        # merge the branch into itself.
        ConfigSetting.objects.set_value("target_branch", _INTEGRATION)
        with patch(_DEFAULT_BRANCH, return_value="main"):
            assert resolve_target_branch(self.ticket, "/repo", branch=_INTEGRATION) == "origin/main"

    def test_the_self_target_guard_ignores_an_origin_prefix(self) -> None:
        # The setting may be written bare or origin-qualified; the guard compares
        # the bare names so one spelling does not defeat it.
        ConfigSetting.objects.set_value("target_branch", f"origin/{_INTEGRATION}")
        with patch(_DEFAULT_BRANCH, return_value="main"):
            assert resolve_target_branch(self.ticket, "/repo", branch=_INTEGRATION) == "origin/main"

    def test_a_different_branch_still_targets_the_configured_one(self) -> None:
        # Anti-vacuity for the guard: it must fire ONLY on the target itself.
        ConfigSetting.objects.set_value("target_branch", _INTEGRATION)
        with patch(_DEFAULT_BRANCH, return_value="main"):
            assert resolve_target_branch(self.ticket, "/repo", branch="feat/other") == f"origin/{_INTEGRATION}"

    def test_an_orphan_branch_with_no_ticket_still_honours_the_setting(self) -> None:
        ConfigSetting.objects.set_value("target_branch", _INTEGRATION)
        with patch(_DEFAULT_BRANCH, return_value="main"):
            assert resolve_target_branch(None, "/repo", branch="feat/x") == f"origin/{_INTEGRATION}"

    def test_an_unresolvable_repo_default_falls_back_to_origin_main(self) -> None:
        with patch(_DEFAULT_BRANCH, side_effect=RuntimeError("not a git repo")):
            assert resolve_target_branch(self.ticket, "/repo", branch="feat/x") == "origin/main"

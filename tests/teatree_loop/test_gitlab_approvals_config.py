# test-path: cross-cutting
"""The GitLab-approval poll-scanner feature flag is DB-home (#2697).

``_gitlab_approvals_enabled`` formerly read ``TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED``
straight off ``os.environ``; it now resolves via the effective-settings tier
(``gitlab_approval_scanner_enabled``). The DB row is the sole source — default
off, on when a row is set.
"""

import pytest
from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.loop.scanner_factory_config import _gitlab_approvals_enabled


class TestGitlabApprovalsEnabled(TestCase):
    @pytest.fixture(autouse=True)
    def _clear_legacy_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_off_by_default_with_no_db_row(self) -> None:
        assert _gitlab_approvals_enabled() is False

    def test_db_row_enables_the_scanner(self) -> None:
        ConfigSetting.objects.set_value("gitlab_approval_scanner_enabled", value=True)
        assert _gitlab_approvals_enabled() is True

    def test_db_row_false_keeps_it_off(self) -> None:
        ConfigSetting.objects.set_value("gitlab_approval_scanner_enabled", value=False)
        assert _gitlab_approvals_enabled() is False

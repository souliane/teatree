"""``github_transport_preset`` gates ``GitHubPollingScanner`` admission (#4795, GitLabApprovalsScanner precedent).

Covers both the SHIP domain fan-out and the legacy per-host fan-out.
"""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.core.models import ConfigSetting
from teatree.loop.domain_jobs import _ship_jobs_for_overlay
from teatree.loop.scanner_host_fanout import _jobs_for_backend_hosts
from teatree.loop.scanners import GitHubPollingScanner


def _backend() -> OverlayBackends:
    overlay = MagicMock()
    overlay.config.get_review_broadcast_channels.return_value = []
    overlay.config.get_review_channel.return_value = ("", "")
    overlay.metadata.get_followup_repos.return_value = []
    overlay.get_workspace_repos.return_value = []
    return OverlayBackends(
        name="teatree",
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=MagicMock(spec=MessagingBackend),
        ready_labels=("ready",),
        overlay=overlay,
    )


def _has_github_polling(jobs: list) -> bool:
    return any(isinstance(job.scanner, GitHubPollingScanner) for job in jobs)


class TestShipDomainAdmitsGitHubPollingByDefault(TestCase):
    def test_polling_default_admits_the_scanner(self) -> None:
        backend = _backend()
        jobs = _ship_jobs_for_overlay(backend, all_backends=(backend,))
        assert _has_github_polling(jobs)

    def test_webhook_preset_excludes_the_scanner(self) -> None:
        ConfigSetting.objects.set_value("github_transport_preset", value="webhook")
        backend = _backend()
        jobs = _ship_jobs_for_overlay(backend, all_backends=(backend,))
        assert not _has_github_polling(jobs)

    def test_only_one_instance_per_overlay_regardless_of_host_count(self) -> None:
        overlay = MagicMock()
        overlay.config.get_review_broadcast_channels.return_value = []
        overlay.config.get_review_channel.return_value = ("", "")
        overlay.metadata.get_followup_repos.return_value = []
        overlay.get_workspace_repos.return_value = []
        backend = OverlayBackends(
            name="teatree",
            hosts=(MagicMock(spec=CodeHostBackend), MagicMock(spec=CodeHostBackend)),
            messaging=MagicMock(spec=MessagingBackend),
            ready_labels=("ready",),
            overlay=overlay,
        )
        jobs = _ship_jobs_for_overlay(backend, all_backends=(backend,))
        assert sum(1 for job in jobs if isinstance(job.scanner, GitHubPollingScanner)) == 1


class TestLegacyPerHostFanoutAdmitsGitHubPollingByDefault(TestCase):
    def test_polling_default_admits_the_scanner(self) -> None:
        backend = _backend()
        jobs = _jobs_for_backend_hosts(backend, "teatree", all_backends=(backend,))
        assert _has_github_polling(jobs)

    def test_webhook_preset_excludes_the_scanner(self) -> None:
        ConfigSetting.objects.set_value("github_transport_preset", value="webhook")
        backend = _backend()
        jobs = _jobs_for_backend_hosts(backend, "teatree", all_backends=(backend,))
        assert not _has_github_polling(jobs)

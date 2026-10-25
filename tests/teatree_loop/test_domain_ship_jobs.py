from unittest.mock import MagicMock, patch

import pytest

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.loop.domain_ship_jobs import _ship_jobs_for_overlay
from teatree.loop.scanners.unaudited_merge import UnauditedMergeScanner
from teatree.types import ScannerError


def _backend(*, host_count: int) -> OverlayBackends:
    overlay = MagicMock()
    overlay.config.get_review_broadcast_channels.return_value = []
    overlay.config.get_review_channel.return_value = ("", "")
    overlay.metadata.get_followup_repos.return_value = []
    overlay.get_workspace_repos.return_value = []
    return OverlayBackends(
        name="example",
        hosts=tuple(MagicMock(spec=CodeHostBackend) for _ in range(host_count)),
        messaging=None,
        ready_labels=(),
        overlay=overlay,
    )


def test_ship_jobs_watch_each_owned_repo() -> None:
    backend = _backend(host_count=1)
    with patch("teatree.loop.domain_ship_jobs.owned_repo_slugs", return_value=("acme/widget",)):
        jobs = _ship_jobs_for_overlay(backend, all_backends=(backend,))
    detectors = [job.scanner for job in jobs if isinstance(job.scanner, UnauditedMergeScanner)]
    assert len(detectors) == 1
    assert detectors[0].repos == ("acme/widget",)


def test_ship_jobs_fail_loudly_without_an_unambiguous_host() -> None:
    backend = _backend(host_count=2)
    with (
        patch("teatree.loop.domain_ship_jobs.owned_repo_slugs", return_value=("acme/widget",)),
        pytest.raises(ScannerError, match="no unambiguous code host"),
    ):
        _ship_jobs_for_overlay(backend, all_backends=(backend,))

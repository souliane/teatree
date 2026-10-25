import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from teatree.config import Autonomy
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.loop.domain_ship_jobs import _ship_jobs_for_overlay
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners.unaudited_merge import UnauditedMergeScanner


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


def _ship_jobs(backend: OverlayBackends, *, autonomy: Autonomy = Autonomy.FULL) -> list[_ScannerJob]:
    with (
        patch("teatree.loop.domain_ship_jobs.owned_repo_slugs", return_value=("acme/widget",)),
        patch(
            "teatree.loop.domain_ship_jobs._effective_settings_for_overlay",
            return_value=SimpleNamespace(autonomy=autonomy),
        ),
        patch("teatree.loop.domain_ship_jobs._user_identity_aliases_for_overlay", return_value=("owner",)),
    ):
        return _ship_jobs_for_overlay(backend, all_backends=(backend,))


def _detectors(backend: OverlayBackends, *, autonomy: Autonomy = Autonomy.FULL) -> list[UnauditedMergeScanner]:
    return [
        job.scanner for job in _ship_jobs(backend, autonomy=autonomy) if isinstance(job.scanner, UnauditedMergeScanner)
    ]


def test_ship_jobs_watch_each_owned_repo_on_a_solo_overlay() -> None:
    detectors = _detectors(_backend(host_count=1))
    assert [(d.repos, d.identities, d.solo_overlay) for d in detectors] == [(("acme/widget",), ("owner",), True)]


def test_ship_jobs_scope_a_collaborative_overlay_to_the_owner_identities() -> None:
    detectors = _detectors(_backend(host_count=1), autonomy=Autonomy.NOTIFY)
    assert [(d.identities, d.solo_overlay) for d in detectors] == [(("owner",), False)]


def test_a_repo_without_an_unambiguous_host_skips_only_its_detector(caplog: pytest.LogCaptureFixture) -> None:
    """Mutant: raise at build time and the whole ship loop is skipped for every overlay."""
    backend = _backend(host_count=2)
    with caplog.at_level(logging.WARNING, logger="teatree.loop.domain_ship_jobs"):
        jobs = _ship_jobs(backend)
    assert jobs
    assert not any(isinstance(job.scanner, UnauditedMergeScanner) for job in jobs)
    assert "no unambiguous code host for acme/widget" in caplog.text


def test_an_unresolvable_watch_scope_leaves_the_rest_of_the_ship_loop_intact(caplog: pytest.LogCaptureFixture) -> None:
    """Mutant: let the scope read raise and the PR sweep that merges is skipped with it."""
    backend = _backend(host_count=1)
    with (
        patch("teatree.loop.domain_ship_jobs.owned_repo_slugs", side_effect=RuntimeError("overlay hook broke")),
        caplog.at_level(logging.ERROR, logger="teatree.loop.domain_ship_jobs"),
    ):
        jobs = _ship_jobs_for_overlay(backend, all_backends=(backend,))
    assert jobs
    assert not any(isinstance(job.scanner, UnauditedMergeScanner) for job in jobs)
    assert "cannot resolve the watch scope for example" in caplog.text

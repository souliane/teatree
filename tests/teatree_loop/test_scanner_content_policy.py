from dataclasses import replace
from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_protocols import MessagingBackend
from teatree.loop.global_scanner_factories import build_default_jobs
from teatree.loop.scanner_content_policy import CONTENT_POLLERS, NOT_FORGE, STATE_POLLERS
from tests.teatree_loop.test_autonomy_loop_wiring import _backend


class TestScannerContentPolicy(TestCase):
    def test_every_wired_scanner_has_a_trigger_class(self) -> None:
        backend = replace(
            _backend(name="t3-teatree", identities=("alice",)), messaging=MagicMock(spec=MessagingBackend)
        )
        jobs = build_default_jobs(backends=[backend])
        names = {type(job.scanner).__name__ for job in jobs}
        unknown = names - CONTENT_POLLERS - STATE_POLLERS - NOT_FORGE
        assert not unknown, f"Unclassified scanners: {', '.join(sorted(unknown))}"
        assert not CONTENT_POLLERS & STATE_POLLERS
        assert not (CONTENT_POLLERS | STATE_POLLERS) & NOT_FORGE

"""Real-provisioning integration test for the bundled teatree overlay.

Two concurrent real git worktrees, each running one real DB-touching test
(as its own ``pytest`` subprocess that does NOT boot Django) and then
booting a real Django ``runserver`` on a distinct OS-assigned port, asserted
reachable via :func:`teatree.core.readiness.run_probes`. No docker — runs in
normal CI. The two servers get distinct ports (never hardcode 8000), proving
the concurrency is genuine; an elapsed-time cap guards against serialization
or a hang (generous enough to survive a loaded CI box).

The DB-touching probe is :func:`test_db_roundtrip` in this module: each
worktree's subprocess runs it by node id, exercising a real Django ORM
round-trip against the test database.
"""

import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.contrib.t3_teatree.overlay import TeatreeOverlay
from teatree.contrib.t3_teatree.overlay import _repo_root as teatree_repo_root
from teatree.core.models import Ticket
from teatree.core.overlay import OverlayBase
from teatree.core.readiness import HTTPProbeSpec, Probe, http_probe

from ._base import ProvisionedWorktree, ProvisioningIntegrationBase, free_tcp_port

_SELF_SETTINGS = "tests.integration.provisioning._self_settings"
_MODULE_PATH = f"{__name__.replace('.', '/')}.py"
_DB_TEST_NODEID = f"{_MODULE_PATH}::DbRoundtripTests::test_db_roundtrip"


class DbRoundtripTests(TestCase):
    """The DB-touching test each worktree's subprocess runs by node id.

    A real Django ORM round-trip against the test database — it must not boot
    a server, so server reachability stays a separate, later assertion.
    """

    def test_db_roundtrip(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", issue_url="https://example.com/prov-self")
        assert Ticket.objects.filter(pk=ticket.pk).exists()


@pytest.mark.integration
@pytest.mark.timeout(60)
class TestTeatreeSelfProvisioning(ProvisioningIntegrationBase):
    CONCURRENT_WORKTREES = 2

    def overlay(self) -> OverlayBase:
        return TeatreeOverlay()

    def workspace_repos(self) -> list[str]:
        return [f"teatree-wt-{i}" for i in range(self.CONCURRENT_WORKTREES)]

    def concurrency(self) -> int:
        return self.CONCURRENT_WORKTREES

    def time_cap_seconds(self) -> float:
        return 40.0

    def db_touch_test_command(self, wt: ProvisionedWorktree) -> Sequence[str]:
        return [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            "no:randomly",
            "-p",
            "no:tach",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-q",
            _DB_TEST_NODEID,
        ]

    def readiness_probes(self, wt: ProvisionedWorktree) -> list[Probe]:
        port = wt.ports["backend"]
        return [
            http_probe(
                name=f"{wt.repo}-routing",
                description="Django runserver is up and routing — webhook view rejects GET with 405",
                spec=HTTPProbeSpec(
                    url=f"http://127.0.0.1:{port}/hooks/slack/",
                    expected_status=405,
                    retries=20,
                    retry_delay=0.25,
                    timeout_seconds=2.0,
                ),
            ),
        ]

    def _start(self, wt: ProvisionedWorktree) -> None:
        port = free_tcp_port()
        wt.ports["backend"] = port
        repo_root = teatree_repo_root()
        server = self._spawn_server(
            [
                sys.executable,
                str(repo_root / "manage.py"),
                "runserver",
                f"127.0.0.1:{port}",
                "--noreload",
                "--skip-checks",
            ],
            cwd=repo_root,
            env_overrides={"DJANGO_SETTINGS_MODULE": _SELF_SETTINGS},
        )
        wt.servers.append(server)

    def test_two_worktrees_provision_serve_concurrently(
        self,
        provisioning_root: tuple[Path, Callable[[Callable[[], None]], None]],
    ) -> None:
        root, register_finalizer = provisioning_root
        worktrees = self.provision_all(root, register_finalizer)
        assert len(worktrees) == self.CONCURRENT_WORKTREES

        start = time.monotonic()
        self.run_concurrently(worktrees)
        elapsed = time.monotonic() - start

        ports = {wt.ports["backend"] for wt in worktrees}
        assert len(ports) == self.CONCURRENT_WORKTREES, "worktrees must bind distinct ports"
        assert 8000 not in ports, "port must be OS-assigned, never the hardcoded default"
        assert elapsed <= self.time_cap_seconds(), f"took {elapsed:.1f}s, cap {self.time_cap_seconds()}s"

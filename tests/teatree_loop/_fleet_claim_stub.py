"""An in-process fleet claim ref: the first acquire of an issue wins, every later one loses.

Issue intake always claims through the cross-instance ref. These tests are about intake,
not the git compare-and-swap, so the round-trip is replaced by the same win-once contract.
"""

from unittest import TestCase, mock

from teatree.core.fleet.claim import Claim, claim_ref


class FleetClaimStub:
    def __init__(self) -> None:
        self.held: set[str] = set()

    def acquire(self, issue_url: str) -> Claim | None:
        if issue_url in self.held:
            return None
        self.held.add(issue_url)
        return Claim(
            work_key=issue_url,
            ref=claim_ref(issue_url),
            sha="a" * 40,
            instance_id="test-instance",
            claimed_at=0.0,
            ttl_seconds=3600.0,
        )

    def install(self, test_case: TestCase) -> "FleetClaimStub":
        patcher = mock.patch("teatree.core.fleet.wire.acquire_issue_claim", side_effect=self.acquire)
        patcher.start()
        test_case.addCleanup(patcher.stop)
        return self

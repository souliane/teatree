"""Tests for the PR-sweep adapter port Protocols (#1248).

The four ports (`PrApiClient`, `MergeKeystone`, `ReviewDispatcher`,
`MergeNotifier`) are `runtime_checkable`, so a conforming adapter matches
`isinstance` while a class missing a required method does not. The scanner
accepts any object satisfying the port, which is what keeps the production
adapters and the test fakes interchangeable.
"""

from teatree.loop.scanners.pr_sweep_ports import MergeKeystone, MergeNotifier, PrApiClient, ReviewDispatcher
from teatree.loop.scanners.pr_sweep_types import PrSummary


class _Api:
    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        _ = slug
        return []

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        _ = (slug, check_name)
        return False

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:
        _ = (slug, pr_id, expected_head_oid)
        return True, "sha"


class _Keystone:
    def merge_clear(self, *, clear_id: int, human_authorized: str = "") -> tuple[bool, str, str, str, str]:
        _ = (clear_id, human_authorized)
        return True, "sha", "", "", ""


class _Dispatcher:
    def enqueue(self, *, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str) -> bool:
        _ = (slug, pr_id, head_sha, pr_url, overlay)
        return True


class _Notifier:
    def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None:
        _ = (slug, pr_id, merged_sha, fallback)

    def flag(self, *, slug: str, pr_id: int, reason: str, url: str) -> None:
        _ = (slug, pr_id, reason, url)


class _MissingMethod:
    """Satisfies none of the ports — used to prove the checks are not vacuous."""


class TestPortsAreRuntimeCheckable:
    def test_conforming_adapters_match_their_ports(self) -> None:
        assert isinstance(_Api(), PrApiClient)
        assert isinstance(_Keystone(), MergeKeystone)
        assert isinstance(_Dispatcher(), ReviewDispatcher)
        assert isinstance(_Notifier(), MergeNotifier)

    def test_class_missing_required_methods_is_rejected(self) -> None:
        stub = _MissingMethod()
        assert not isinstance(stub, PrApiClient)
        assert not isinstance(stub, MergeKeystone)
        assert not isinstance(stub, ReviewDispatcher)
        assert not isinstance(stub, MergeNotifier)

    def test_ports_do_not_cross_match(self) -> None:
        # An MergeNotifier is not a PrApiClient — the ports are distinct contracts.
        assert not isinstance(_Notifier(), PrApiClient)
        assert not isinstance(_Api(), MergeNotifier)

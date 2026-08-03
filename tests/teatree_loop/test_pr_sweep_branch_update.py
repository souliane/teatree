"""Tests for the sweep's stale-base merge-update remedy (#4063).

Covers the classification predicate (:func:`red_required_at_stale_base`) and the
``gh``-backed write it drives (:meth:`GhPrApiClient.update_pr_branch`). The
scanner-level decision ladder and the three bounds are pinned in
``test_pr_sweep_scanner.py``.
"""

from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient
from teatree.loop.scanners.pr_sweep_decision import red_required_at_stale_base

SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"


class TestRedRequiredAtStaleBase:
    """The rule: a required red is UNKNOWN when the base it judged has moved."""

    def test_red_on_behind_branch_is_stale(self) -> None:
        assert red_required_at_stale_base({"test (3.13)"}, behind_main=True) is True

    def test_repo_state_red_on_behind_branch_is_stale(self) -> None:
        assert red_required_at_stale_base({"blueprint-cross-pr"}, behind_main=True) is True

    def test_red_on_up_to_date_branch_is_the_branchs_own_verdict(self) -> None:
        assert red_required_at_stale_base({"test (3.13)"}, behind_main=False) is False

    def test_no_failing_required_check_is_never_stale(self) -> None:
        assert red_required_at_stale_base(set(), behind_main=True) is False


class _CapturingClient(GhPrApiClient):
    """A client whose ``gh`` calls are captured instead of executed."""

    __slots__ = ()

    argv_log: "list[list[str]]" = []  # noqa: RUF012 — shared capture buffer; the slotted base forbids an instance field.
    rc: int = 0

    def _run_gh(self, argv: list[str]) -> tuple[int, str, str]:
        type(self).argv_log.append(argv)
        return type(self).rc, "", "expected_head_sha does not match"


class TestGhUpdatePrBranch:
    """The write is SHA-bound, so a force-push in the TOCTOU window is refused."""

    def _client(self, *, rc: int) -> _CapturingClient:
        _CapturingClient.argv_log = []
        _CapturingClient.rc = rc
        return _CapturingClient(token="")

    def test_shells_the_sha_bound_update_branch_endpoint(self) -> None:
        client = self._client(rc=0)

        assert client.update_pr_branch(slug=SLUG, pr_id=4029, expected_head_oid=HEAD) is True

        argv = _CapturingClient.argv_log[0]
        assert argv[:4] == ["api", "--method", "PUT", f"repos/{SLUG}/pulls/4029/update-branch"]
        assert f"expected_head_sha={HEAD}" in argv

    def test_non_zero_rc_reports_refusal(self) -> None:
        client = self._client(rc=1)

        assert client.update_pr_branch(slug=SLUG, pr_id=4029, expected_head_oid=HEAD) is False
